"""kittrial-5bb.522 slice 2 r2: the pre-review's shoulds and notes 1 and 3 (review-522s2, 2026-11-08).

web/js/app.js, views/project.js and routes.js run under Node (tests/web_project_sections_522s2r2.mjs, with
tests/web_dom_shim.mjs) against a stubbed transport that records every request. One run gives all
observations; each rule has its own named test.

The rules:
- Should 1: an unusable project's members link goes to People; credentials and archive stay on Settings.
- Should 2: the Who-coordinates line on People and on Agents names where roles and where grants change.
- Should 3: the Settings lede is true for each role; a coordinator who can save Claiming is not told that
  only owners can change the page.
- Should 4: a switch from each of a project's People, Agents and Settings to another project and back keeps
  the section, says nothing, and each page reads only its route's project, even with the other project
  remembered first.
- Note 1: a lost session with nobody signed in keeps the person the page was for; the next different person
  to sign in lands at home, the same person back on their page.
- Note 3: the hand-over walkthrough is linked from People and Agents as from Settings.
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
            [node, str(TESTS / 'web_project_sections_522s2r2.mjs'), (TESTS / 'web_dom_shim.mjs').as_uri(),
             (WEB / 'app.js').as_uri(), (WEB / 'views' / 'project.js').as_uri(), (WEB / 'routes.js').as_uri()],
            capture_output=True, text=True, timeout=300)
        case.assertEqual(0, done.returncode, done.stderr[-3000:])
        _RESULT = json.loads(done.stdout.strip().splitlines()[-1])
    return _RESULT


class UnusableProjectLinks(unittest.TestCase):

    def test_members_go_to_people_and_credentials_and_archive_to_settings(self):
        links = [link for link in observations(self)['unusableLinks'] if link[0].startswith('#/p/')]
        self.assertEqual([['#/p/delta/people', 'Members'], ['#/p/delta/settings', 'Credentials and archive']], links)


class WhoCoordinatesLine(unittest.TestCase):

    def test_the_line_names_people_for_roles_and_agents_for_grants_on_both_pages(self):
        line = ("Read only: owners change roles on People and grants on Agents; "
                "operators change the installation's rules on the server")
        seen = observations(self)['coordinationLine']
        self.assertEqual({'people': [line], 'agents': [line]}, seen)


class SettingsLede(unittest.TestCase):

    def test_each_role_reads_a_true_sentence(self):
        seen = observations(self)['settingsLede']
        self.assertEqual('How work is claimed and approved, credentials, activity and archiving.', seen['alpha']['lede'])
        self.assertEqual('Only project owners can change these.', seen['beta']['lede'])
        self.assertEqual(0, seen['beta']['claimingSave'])

    def test_a_coordinator_who_can_save_claiming_is_not_told_only_owners_can(self):
        gamma = observations(self)['settingsLede']['gamma']
        self.assertEqual(1, gamma['claimingSave'], 'the coordinator is offered Save on Claiming')
        self.assertEqual('You can change how work is handed out; only project owners can change the rest.', gamma['lede'])


class SwitchRoundTrip(unittest.TestCase):

    def test_each_section_switches_to_the_other_project_and_back_reading_only_the_routes_project(self):
        seen = observations(self)['switchRound']
        titles = {'people': 'People', 'agents': 'Agents', 'settings': 'Settings'}
        for section, title in titles.items():
            with self.subTest(section=section):
                round_trip = seen[section]
                self.assertEqual(
                    [(f'/p/alpha/{section}', title, ['alpha']),
                     (f'/p/beta/{section}', title, ['beta']),
                     (f'/p/alpha/{section}', title, ['alpha'])],
                    [(step['at'], step['title'], step['read']) for step in round_trip['steps']])
                self.assertEqual([], round_trip['toasts'], 'a kept section says nothing')


class LostSessionKeepsItsPerson(unittest.TestCase):

    def test_a_second_lost_session_with_nobody_signed_in_keeps_the_page_for_its_person(self):
        seen = observations(self)
        self.assertEqual('/p/alpha/people', seen['lostTwiceThenSame'])

    def test_a_second_lost_session_with_nobody_signed_in_sends_another_person_home(self):
        seen = observations(self)
        self.assertEqual('/', seen['lostTwiceThenOther'])


class WalkthroughLinks(unittest.TestCase):

    def test_people_agents_and_settings_each_link_the_walkthrough(self):
        seen = observations(self)['walkthrough']
        self.assertEqual({section: ['#/p/alpha/handover'] for section in ('people', 'agents', 'settings')}, seen)


if __name__ == '__main__':
    unittest.main()
