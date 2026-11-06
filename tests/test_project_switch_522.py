"""kittrial-5bb.522 slice 1: the project switch is orthogonal to the other menu entries.

web/js/app.js, routes.js and views/project.js run under Node (tests/web_project_switch_522.mjs, with
tests/web_dom_shim.mjs, a fake location/history and a fake storage) against a stubbed transport. One run gives all
observations; each rule has its own named test.

The rules (docs/design/PROJECT_CROSS_SECTION_DESIGN.md, section 7, slice 1):
(a) switching project keeps the section and the view's filters, never a key that names a record of the old project
    nor a value that names one (an id in q; a status, review or requirements value outside its vocabulary);
(b) a section the target does not give this person falls back to its Tasks list, says so once, keeps the filters;
(c) a task page switches to the target's Tasks list with nothing of its query; a room, requirement, record or
    decision page to the target's list of that kind where served; each says the true reason;
(d) the last project a person opened is remembered per person, shown on pages without a project, forgotten when
    no longer listed, cleared at sign-out; the URL always wins on a project page;
(e) a deep link opened signed out is where the person lands after signing in, for the person it was opened for;
(f) the Tasks and Reviews filters are the URL's query, read on load and written back without a new history entry;
    a write belongs to the render that asked for it (the search box's timer never writes onto the next page).

Two Node runs: web_project_switch_522.mjs (one set of observations, a simple fake browser) and
web_project_switch_522_browser.mjs (a stricter browser on WHATWG URL with a real history stack; one "ok name" line
per test, and BrowserBehaviour below has one test per line).
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
        self.assertEqual('changes-requested', seen['deepLinkOpened'])      # the page rendered, not only the hash
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


_BROWSER = None


def browser_lines(case):
    global _BROWSER
    if _BROWSER is None:
        node = shutil.which('node')
        if not node:
            case.skipTest('node is not installed')
        done = subprocess.run(
            [node, str(TESTS / 'web_project_switch_522_browser.mjs'), (TESTS / 'web_dom_shim.mjs').as_uri(),
             (WEB / 'app.js').as_uri(), (WEB / 'routes.js').as_uri(), (WEB / 'views' / 'project.js').as_uri()],
            capture_output=True, text=True, timeout=300)
        _BROWSER = {}
        for line in done.stdout.splitlines():
            if line.startswith(('ok   ', 'FAIL ')):
                name = line[5:].split(':', 1)[0] if line.startswith('FAIL ') else line[5:]
                _BROWSER[name.strip()] = line
        _BROWSER[''] = done.stderr[-2000:]
    return _BROWSER


class BrowserBehaviour(unittest.TestCase):
    """One test per line of web_project_switch_522_browser.mjs; a missing line fails too."""

    def check(self, name):
        lines = browser_lines(self)
        self.assertIn(name, lines, 'the test did not run; stderr: ' + lines[''])
        self.assertTrue(lines[name].startswith('ok   '), lines[name])

    # the review's own tests, T1-T10
    def test_t1_an_id_of_the_old_project_in_a_filter_value_does_not_cross(self):
        self.check('t1_an_id_of_the_old_project_in_a_filter_value_does_not_cross')

    def test_t2_back_restores_a_filtered_list_and_a_link_without_the_filter_resets_it(self):
        self.check('t2_back_restores_a_filtered_list_and_a_link_without_the_filter_resets_it')

    def test_t3_a_deep_link_spelling_an_external_url_stays_on_this_origin_after_sign_in(self):
        self.check('t3_a_deep_link_spelling_an_external_url_stays_on_this_origin_after_sign_in')

    def test_t4_the_search_timer_does_not_rewrite_the_url_of_the_page_opened_meanwhile(self):
        self.check('t4_the_search_timer_does_not_rewrite_the_url_of_the_page_opened_meanwhile')

    def test_t5_a_failed_read_of_the_project_list_does_not_forget_the_project(self):
        self.check('t5_a_failed_read_of_the_project_list_does_not_forget_the_project')

    def test_t6_a_room_page_or_a_requirement_page_keeps_nothing_of_its_record(self):
        self.check('t6_a_room_page_or_a_requirement_page_keeps_nothing_of_its_record')

    def test_t7_after_sign_in_a_deep_link_shows_its_page(self):
        self.check('t7_after_sign_in_a_deep_link_shows_its_page')

    def test_t8_each_tasks_control_writes_the_url_and_all_round_trips(self):
        self.check('t8_each_tasks_control_writes_the_url_and_all_round_trips')

    def test_t9_unknown_and_alias_values_on_reviews_and_tasks(self):
        self.check('t9_unknown_and_alias_values_on_reviews_and_tasks')

    def test_t10_a_project_page_that_is_not_listed_does_not_overwrite_the_remembered_project(self):
        self.check('t10_a_project_page_that_is_not_listed_does_not_overwrite_the_remembered_project')

    # m1: values
    def test_m1_q_crosses_only_without_an_id_shaped_word(self):
        self.check('m1_q_crosses_only_without_an_id_shaped_word')

    def test_m1_status_review_requirements_cross_only_as_words_of_their_vocabulary(self):
        self.check('m1_status_review_requirements_cross_only_as_words_of_their_vocabulary')

    def test_m1_the_switcher_applies_it_to_the_live_url(self):
        self.check('m1_the_switcher_applies_it_to_the_live_url')

    # m2: a write belongs to the render that asked
    def test_m2_a_rail_link_within_the_search_window_keeps_its_own_url(self):
        self.check('m2_a_rail_link_within_the_search_window_keeps_its_own_url')

    def test_m2_reviews_keeps_its_review_and_proposal_keys(self):
        self.check('m2_reviews_keeps_its_review_and_proposal_keys')

    def test_m2_a_switch_within_the_search_window_writes_nothing_onto_the_new_project(self):
        self.check('m2_a_switch_within_the_search_window_writes_nothing_onto_the_new_project')

    def test_m2_a_task_page_opened_within_the_search_window_keeps_its_url(self):
        self.check('m2_a_task_page_opened_within_the_search_window_keeps_its_url')

    def test_m2_signing_out_within_the_search_window_leaves_home_alone(self):
        self.check('m2_signing_out_within_the_search_window_leaves_home_alone')

    def test_m2_a_lost_session_within_the_search_window_writes_nothing(self):
        self.check('m2_a_lost_session_within_the_search_window_writes_nothing')

    def test_m2_a_search_left_alone_is_still_written(self):
        self.check('m2_a_search_left_alone_is_still_written')

    # s1
    def test_s1_a_503_keeps_the_key_and_a_successful_list_without_it_forgets(self):
        self.check('s1_a_503_keeps_the_key_and_a_successful_list_without_it_forgets')

    # s2
    def test_s2_handover_and_notes_keep_their_section_on_a_switch(self):
        self.check('s2_handover_and_notes_keep_their_section_on_a_switch')

    def test_s2_a_record_page_switches_to_its_list_and_says_why(self):
        self.check('s2_a_record_page_switches_to_its_list_and_says_why')

    def test_s2_program_without_a_program_in_the_target_says_so_by_name(self):
        self.check('s2_program_without_a_program_in_the_target_says_so_by_name')

    # s3
    def test_s3_back_and_forward_across_a_switch(self):
        self.check('s3_back_and_forward_across_a_switch')

    def test_s3_the_controls_drive_the_url_and_the_switch_carries_what_they_wrote(self):
        self.check('s3_the_controls_drive_the_url_and_the_switch_carries_what_they_wrote')

    def test_s3_the_status_words_and_inherited_names_in_the_url(self):
        self.check('s3_the_status_words_and_inherited_names_in_the_url')

    # r3
    def test_m1b_a_search_left_behind_is_said_once_and_alone_or_with_a_fallback_sentence(self):
        self.check('m1b_a_search_left_behind_is_said_once_and_alone_or_with_a_fallback_sentence')

    def test_m1b_with_a_fallback_the_two_sentences_share_one_toast(self):
        self.check('m1b_with_a_fallback_the_two_sentences_share_one_toast')

    def test_m1b_no_search_no_line(self):
        self.check('m1b_no_search_no_line')

    def test_s1a_a_new_render_on_the_same_path_does_not_take_the_old_timers_write(self):
        self.check('s1a_a_new_render_on_the_same_path_does_not_take_the_old_timers_write')

    def test_s1a_the_rail_tasks_link_from_a_closed_list_inside_the_window(self):
        self.check('s1a_the_rail_tasks_link_from_a_closed_list_inside_the_window')

    def test_s1b_an_archived_projects_page_does_not_become_the_remembered_project(self):
        self.check('s1b_an_archived_projects_page_does_not_become_the_remembered_project')

    def test_n2_a_double_click_on_sign_out_still_sends_the_next_person_home(self):
        self.check('n2_a_double_click_on_sign_out_still_sends_the_next_person_home')

    def test_n3_an_inherited_name_as_a_section_puts_no_code_in_the_toast(self):
        self.check('n3_an_inherited_name_as_a_section_puts_no_code_in_the_toast')

    # notes
    def test_n4_the_group_asked_for_says_none_even_when_it_is_needs_operator_attention(self):
        self.check('n4_the_group_asked_for_says_none_even_when_it_is_needs_operator_attention')

    def test_n1_a_session_lost_in_another_tab_forgets_the_project_and_nothing_is_written_back(self):
        self.check('n1_a_session_lost_in_another_tab_forgets_the_project_and_nothing_is_written_back')

    def test_n2_the_next_person_does_not_land_on_the_last_ones_url_after_a_lost_session(self):
        self.check('n2_the_next_person_does_not_land_on_the_last_ones_url_after_a_lost_session')

    def test_n2_the_same_person_returns_to_the_page_after_a_lost_session(self):
        self.check('n2_the_same_person_returns_to_the_page_after_a_lost_session')

    def test_n2_back_on_the_login_page_after_sign_out_does_not_hand_the_next_person_the_last_url(self):
        self.check('n2_back_on_the_login_page_after_sign_out_does_not_hand_the_next_person_the_last_url')

    def test_n2_a_deep_link_opened_with_nobody_signed_in_is_for_whoever_signs_in(self):
        self.check('n2_a_deep_link_opened_with_nobody_signed_in_is_for_whoever_signs_in')


class FilterCssAndSource(unittest.TestCase):
    def test_the_filters_row_has_a_css_rule(self):
        css = (TESTS.parent / 'web' / 'css' / 'app.css').read_text(encoding='utf-8')
        self.assertIn('\n.filters {', css)
        self.assertIn('flex-wrap: wrap', css[css.index('\n.filters {'):][:200])

    def test_records_state_is_not_a_carried_key(self):
        # No page writes Records' `state`, so no switch carries it (n5).
        text = (WEB / 'routes.js').read_text(encoding='utf-8')
        line = [l for l in text.splitlines() if l.startswith('export const SWITCH_FILTER_KEYS')][0]
        self.assertNotIn("'state'", line)


if __name__ == '__main__':
    unittest.main()
