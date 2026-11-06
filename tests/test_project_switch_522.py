"""kittrial-5bb.522 slice 1: the project switch is orthogonal to the other menu entries.

web/js/app.js, routes.js and views/project.js run under Node (tests/web_project_switch_522.mjs, with
tests/web_dom_shim.mjs, a fake location/history and a fake storage) against a stubbed transport. One run gives all
observations; each rule has its own named test.

The rules (docs/design/PROJECT_CROSS_SECTION_DESIGN.md, section 7, slice 1):
(a) switching project keeps the section and the view's filters, never a key that names a record of the old project;
(b) a section the target does not give this person falls back to its Tasks list, says so once, keeps the filters;
(c) a task page switches to the target's Tasks list with nothing of its query;
(d) the last project a person opened is remembered per person, shown on pages without a project, forgotten when
    no longer listed, cleared at sign-out; the URL always wins on a project page;
(e) a deep link opened signed out is where the person lands after signing in;
(f) the Tasks and Reviews filters are the URL's query, read on load and written back without a new history entry.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
WEB = TESTS.parent / 'web' / 'js'
_RESULT = None


def observations(case):
    global _RESULT
    if _RESULT is None:
        node = shutil.which('node')
        if not node:
            case.skipTest('node is not installed')
        done = subprocess.run(
            [node, str(TESTS / 'web_project_switch_522.mjs'), (TESTS / 'web_dom_shim.mjs').as_uri(),
             (WEB / 'app.js').as_uri(), (WEB / 'routes.js').as_uri(), (WEB / 'views' / 'project.js').as_uri()],
            capture_output=True, text=True, timeout=300)
        case.assertEqual(0, done.returncode, done.stderr[-3000:])
        _RESULT = json.loads(done.stdout.strip().splitlines()[-1])
    return _RESULT


def route(hash_value):
    return hash_value[1:] if hash_value.startswith('#') else hash_value


class SwitchKeepsTheSectionAndTheFilters(unittest.TestCase):
    """(a)"""

    def test_reviews_with_a_filter_switches_to_the_same_reviews_with_the_same_filter(self):
        seen = observations(self)
        self.assertEqual('/p/beta/reviews?review=changes-requested', seen['switchTargets']['reviewsWithFilter']['path'])
        self.assertEqual('/p/beta/reviews?review=changes-requested', route(seen['switchKeeps']['hash']))

    def test_a_key_naming_a_record_of_the_old_project_is_not_carried(self):
        seen = observations(self)
        self.assertNotIn('proposal', seen['switchTargets']['recordKeyDropped']['path'])
        self.assertNotIn('proposal', seen['switchKeeps']['hash'])

    def test_the_tasks_filters_survive_the_switch(self):
        seen = observations(self)
        self.assertEqual('/p/beta?status=closed&q=wish&requirements=1', seen['switchTargets']['tasksFilters']['path'])
        self.assertEqual('/p/beta?status=closed&q=wish&review=changes-requested&requirements=1',
                         route(seen['tasksSwitch']))


class SwitchFallsBackToTasks(unittest.TestCase):
    """(b)"""

    def test_a_section_the_target_does_not_give_this_person_opens_its_tasks_and_says_so_once(self):
        seen = observations(self)
        self.assertEqual('/p/beta?status=closed', route(seen['switchFallsBack']['hash']))
        self.assertEqual(['Beta has no Set up page, so it opened on Tasks.'], seen['switchFallsBack']['toasts'])
        self.assertTrue(seen['switchTargets']['missingSection']['fellBack'])


class SwitchFromATaskPage(unittest.TestCase):
    """(c)"""

    def test_a_task_page_switches_to_the_targets_tasks_list_without_its_query(self):
        seen = observations(self)
        self.assertEqual('/p/beta', route(seen['switchFromTask']['hash']))
        self.assertEqual('/p/beta', seen['switchTargets']['taskPage']['path'])
        self.assertEqual(['Task pages belong to one project, so Beta opened on Tasks.'], seen['switchFromTask']['toasts'])

    def test_a_page_without_a_project_switches_to_the_targets_tasks(self):
        self.assertEqual({'path': '/p/beta', 'fellBack': False, 'section': ''},
                         observations(self)['switchTargets']['globalPage'])


class TheLastProjectIsRemembered(unittest.TestCase):
    """(d)"""

    def test_before_any_project_is_opened_the_switcher_asks_to_choose(self):
        self.assertEqual({'selected': '', 'sections': []}, observations(self)['freshGlobal'])

    def test_opening_a_project_page_remembers_it_and_a_page_without_a_project_shows_it(self):
        seen = observations(self)
        self.assertEqual('alpha', seen['afterProjectPage'])
        shown = seen['globalShowsRemembered']
        self.assertEqual(('alpha', 'alpha'), (shown['selected'], shown['stored']))
        self.assertIn('#/p/alpha/reviews', shown['sections'])
        self.assertEqual('beta', seen['switchKeeps']['stored'])            # a switch remembers the new one

    def test_it_is_kept_per_person_and_forgotten_when_no_longer_listed_or_archived(self):
        seen = observations(self)
        self.assertEqual(['alpha', 'beta'], seen['twoPeople'])
        self.assertEqual([None, None], seen['staleForgotten'])          # not shown, and removed
        self.assertIsNone(seen['archivedNotShown'])
        self.assertIsNone(seen['noStorage'])

    def test_the_url_wins_on_a_project_page(self):
        self.assertEqual({'selected': 'alpha', 'stored': 'alpha'}, observations(self)['urlWins'])

    def test_sign_out_forgets_it_and_sends_the_next_person_home(self):
        self.assertEqual({'stored': None, 'hash': '#/'}, observations(self)['signOut'])


class DeepLinkAfterSignIn(unittest.TestCase):
    """(e)"""

    def test_a_deep_link_opened_signed_out_is_where_the_person_lands(self):
        seen = observations(self)
        self.assertEqual('/p/alpha/reviews?review=changes-requested', route(seen['deepLink']))
        self.assertEqual('/', route(seen['noDeepLink']))


class FiltersLiveInTheUrl(unittest.TestCase):
    """(f), .502 section 1.9"""

    def test_the_tasks_page_reads_its_filters_from_the_url(self):
        read = observations(self)['tasksRead']
        self.assertEqual('GET /v1/projects/alpha/tasks?limit=10&status=closed&q=wish&review_state=approved', read['call'])
        self.assertEqual(('wish', True, 'approved', ['Closed']),
                         (read['search'], read['requirements'], read['review'], read['pressed']))

    def test_a_tasks_filter_change_is_written_to_the_url_without_a_history_entry_or_a_re_render(self):
        written = observations(self)['tasksWritten']
        self.assertEqual('/p/alpha?status=closed&q=wish&review=changes-requested&requirements=1', route(written['hash']))
        self.assertEqual((1, 0, 0), (written['replaced'], written['pushed'], written['rerendered']))

    def test_the_reviews_page_shows_the_group_the_url_names_and_writes_it_back(self):
        seen = observations(self)
        self.assertEqual(['g-changes-requested'], seen['reviewsRead']['shown'])
        self.assertEqual('changes-requested', seen['reviewsRead']['select'])
        self.assertEqual('/p/alpha/reviews', route(seen['reviewsWritten']['hash']))
        self.assertEqual(['g-awaiting-review', 'g-changes-requested'], seen['reviewsWritten']['shown'])


if __name__ == '__main__':
    unittest.main()
