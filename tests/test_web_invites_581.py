"""kittrial-5bb.581: invites for people who already have an account, and the web pieces around them.

web/js/views/invites.js, views/project.js, api.js and app.js run under Node (tests/web_invites_581.mjs, with
tests/web_dom_shim.mjs) against stubbed APIs that record every request. One run gives all observations; each rule
has its own named test.

The rules:
- the invite panel offers "Someone new" (name and username, no role) and "Someone already on Orchestra" (username
  and a role; Owner only for a superuser), and an agent box whose role is Worker or Reviewer;
- a person who may invite sees the panel while the switch is off, with the server's sentence; it is hidden only from
  a person who may not invite and has no invites;
- the overview and People heads carry "Invite someone or add an agent" for an owner or a superuser of a project that
  is not archived;
- the invite page for an existing account signs in (or uses the session) and accepts; it says nothing about the
  account's memberships before the accept, and after it says what was added, raised or kept;
- a new person's invite page is unchanged (.578);
- an answer from another build shows "Orchestra was upgraded. Reload." once.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
WEB = TESTS.parent / 'web' / 'js'
_RESULT = None

TOKEN = 'tok-581-abcdefghijklmnopqrstuvwxyz'
DONE_LINES = ['Added as contributor in North', 'Raised from viewer to coordinator in South',
              'You keep your owner role in West']


def observations(case):
    global _RESULT
    if _RESULT is None:
        node = shutil.which('node')
        if not node:
            case.skipTest('node is not installed')
        done = subprocess.run(
            [node, str(TESTS / 'web_invites_581.mjs'), (TESTS / 'web_dom_shim.mjs').as_uri(),
             (WEB / 'api.js').as_uri(), (WEB / 'views' / 'invites.js').as_uri(),
             (WEB / 'views' / 'project.js').as_uri(), (WEB / 'app.js').as_uri()],
            capture_output=True, text=True, timeout=300)
        case.assertEqual(0, done.returncode, done.stderr[-3000:])
        _RESULT = json.loads(done.stdout.strip().splitlines()[-1])
    return _RESULT


class ThePanelForm(unittest.TestCase):

    def test_the_panel_is_titled_invite_someone_or_add_an_agent_and_offers_the_two_kinds_of_person(self):
        panel = observations(self)['panel']
        self.assertEqual('Invite someone or add an agent', panel['title'])
        self.assertEqual(['Someone new', 'Someone already on Orchestra'], panel['personChoices'])
        self.assertFalse(panel['newFieldsHiddenAtFirst'])
        self.assertTrue(panel['existingFieldsHiddenAtFirst'])

    def test_a_new_person_is_sent_with_name_and_username_and_no_role(self):
        body = observations(self)['panel']['newBody']
        self.assertEqual({'display_name': 'Lena Park', 'username': 'lena', 'projects': ['north'], 'expires_hours': 72}, body)
        self.assertNotIn('role', body)
        self.assertNotEqual('existing', body.get('person'))

    def test_an_existing_person_is_sent_with_username_without_at_and_the_role_and_no_name(self):
        panel = observations(self)['panel']
        self.assertTrue(panel['newFieldsHiddenForExisting'])
        self.assertFalse(panel['existingFieldsHiddenForExisting'])
        self.assertEqual({'person': 'existing', 'username': 'ivy', 'role': 'coordinator', 'projects': ['north'],
                          'expires_hours': 72}, panel['existingBody'])
        self.assertNotIn('display_name', panel['existingBody'])

    def test_the_existing_role_defaults_to_contributor_and_says_a_higher_role_is_kept(self):
        panel = observations(self)['panel']
        self.assertEqual('contributor', panel['roleDefault'])
        self.assertEqual('contributor', panel['existingDefaultBody']['role'])
        self.assertIn('A higher role they already have is kept.', panel['existingShowsRoleHint'])

    def test_owner_is_offered_only_to_a_superuser(self):
        panel = observations(self)['panel']
        self.assertEqual(['viewer', 'contributor', 'coordinator'], panel['roleOptionsOwner'])
        self.assertEqual(['viewer', 'contributor', 'coordinator', 'owner'], panel['roleOptionsSuperuser'])

    def test_the_agent_role_select_sends_reviewer_when_chosen_and_worker_by_default(self):
        panel = observations(self)['panel']
        self.assertEqual(['worker', 'reviewer'], panel['agentRoleOptions'])
        self.assertEqual({'name': 'Lena review bot', 'project': 'north', 'role': 'reviewer', 'claim_limit': 1},
                         panel['agentReviewerBody']['agent'])
        self.assertEqual('worker', panel['agentDefaultBody']['agent']['role'])

    def test_the_agent_box_reads_also_add_an_agent_and_names_the_agents_page(self):
        panel = observations(self)['panel']
        self.assertIn('Also add an agent for them', panel['legendTexts'])
        self.assertNotIn('Also pre-approve one worker agent for them', panel['legendTexts'])
        self.assertEqual(['A worker or reviewer only: a coordinator grant is given on the project’s Agents page '
                          'after they sign in.'], panel['agentNote'])


class ThePanelOffState(unittest.TestCase):

    def test_a_person_who_may_invite_sees_the_panel_with_the_off_sentence_while_the_switch_is_off(self):
        seen = observations(self)['off']['mayInvite']
        self.assertFalse(seen['hidden'])
        self.assertEqual(['Invites are switched off on this installation; a superuser can switch them on.'], seen['offNote'])
        self.assertIn('Invite someone or add an agent', seen['text'])
        self.assertEqual(0, seen['forms'])

    def test_a_person_who_may_not_invite_and_has_no_invites_sees_no_panel(self):
        off = observations(self)['off']
        self.assertEqual({'hidden': True, 'children': 0}, off['mayNotInvite'])
        self.assertEqual({'hidden': True, 'children': 0}, off['mayNotInviteSwitchOn'])


class ThePanelList(unittest.TestCase):

    def test_a_used_invite_to_an_existing_account_says_what_it_did_in_each_project(self):
        seen = observations(self)['list']
        self.assertEqual('@ivy (existing account)', seen['existingTitle'])
        self.assertEqual(['added as contributor in North', 'kept owner in South', 'raised from viewer to contributor in East'],
                         seen['membershipLines'])
        self.assertEqual('Lena Park', seen['newTitle'])
        self.assertEqual(0, seen['newMembershipLines'])

    def test_the_invite_query_focuses_the_panels_first_field_and_nothing_else_does(self):
        focus = observations(self)['focus']
        self.assertTrue(focus['asked'])
        self.assertTrue(focus['notAskedStaysOnBody'])


class TheHeadAction(unittest.TestCase):

    def check(self, page):
        head = observations(self)['head'][page]
        action = {'count': 1, 'href': '#/p/alpha/people?invite=1', 'label': 'Invite someone or add an agent'}
        self.assertEqual(action, {k: head['owner'][k] for k in action})
        self.assertEqual(1, head['superuser']['count'])
        self.assertEqual('#/p/delta/people?invite=1', head['superuser']['href'])
        self.assertEqual(0, head['contributor']['count'])
        self.assertEqual(0, head['archived']['count'])

    def test_the_overview_head_shows_the_action_to_an_owner_and_a_superuser_only_and_keeps_new_task(self):
        self.check('overview')
        head = observations(self)['head']['overview']
        self.assertEqual(1, head['owner']['newTask'])
        self.assertEqual(1, head['contributor']['newTask'])

    def test_the_people_head_shows_the_action_to_an_owner_and_a_superuser_only(self):
        self.check('people')

    def test_a_contributor_gets_no_invite_action(self):
        head = observations(self)['head']
        self.assertEqual(0, head['overview']['contributor']['count'])
        self.assertEqual(0, head['people']['contributor']['count'])

    def test_an_archived_project_gets_no_invite_action(self):
        head = observations(self)['head']
        self.assertEqual(0, head['overview']['archived']['count'])
        self.assertEqual(0, head['people']['archived']['count'])


class TheInvitePageForAnExistingAccount(unittest.TestCase):

    def test_signed_out_it_signs_in_as_the_named_account_and_accepts_without_navigating(self):
        seen = observations(self)['page']['signedOut']
        self.assertEqual(['Olga invites @ivy to North, South, West.'], seen['intro'])
        self.assertEqual('ivy', seen['username'])
        self.assertTrue(seen['readonly'])
        self.assertEqual(0, seen['acceptButtons'])
        self.assertEqual([['viewInvite', TOKEN], ['login', 'ivy', 'correct horse'], ['acceptInvite', TOKEN]], seen['calls'])
        self.assertNotIn(['signedIn'], seen['calls'])
        self.assertEqual('ivy', seen['me'])
        self.assertEqual('Done', seen['title'])
        self.assertEqual(DONE_LINES, seen['lines'])

    def test_signed_in_as_the_named_account_accept_calls_accept_invite_with_the_token(self):
        seen = observations(self)['page']['named']
        self.assertEqual(['Accept'], seen['acceptButtons'])
        self.assertEqual(0, seen['signInForms'])
        self.assertEqual([['viewInvite', TOKEN], ['acceptInvite', TOKEN]], seen['calls'][:2])
        self.assertEqual('Done', seen['title'])

    def test_after_accept_each_project_says_added_raised_or_kept_and_links_to_the_first(self):
        seen = observations(self)['page']['named']
        self.assertEqual(DONE_LINES, seen['lines'])
        self.assertEqual('Go to North', seen['goLabel'])
        self.assertEqual('#/p/north', seen['goHref'])
        self.assertEqual(['go', '/p/north'], seen['calls'][-1])

    def test_signed_in_as_another_account_it_names_the_account_and_offers_no_accept(self):
        seen = observations(self)['page']['other']
        self.assertEqual(['This invite is for @ivy. Sign out and sign in as @ivy to accept it.'], seen['hint'])
        self.assertEqual(0, seen['acceptButtons'])

    def test_signing_out_there_keeps_the_invite_and_shows_the_sign_in_form(self):
        seen = observations(self)['page']['other']
        self.assertEqual({'me': None, 'hasPassword': True, 'username': 'ivy'}, seen['afterSignOut'])
        self.assertEqual([['viewInvite', TOKEN], ['logout'], ['login', 'ivy', 'correct horse'], ['acceptInvite', TOKEN]],
                         seen['calls'])
        self.assertEqual('Done', seen['title'])

    def test_nothing_about_current_memberships_is_said_before_the_accept(self):
        page = observations(self)['page']
        for case in ('signedOut', 'named', 'other'):
            with self.subTest(case=case):
                self.assertEqual([], page[case]['membershipWordsBefore'])

    def test_an_ended_session_shows_the_sign_in_form_again(self):
        seen = observations(self)['page']['sessionEnded']
        self.assertIsNone(seen['me'])
        self.assertTrue(seen['hasPassword'])
        self.assertEqual('You are invited', seen['title'])

    def test_a_refusal_shows_the_servers_sentence(self):
        seen = observations(self)['page']['refused']
        self.assertEqual([{'hidden': False, 'text': 'This invite link cannot be used.'}], seen['alert'])
        self.assertEqual('You are invited', seen['title'])

    def test_the_agent_line_names_the_agents_role(self):
        self.assertEqual(['Olga invites @ivy to North, South, West and adds a reviewer agent “Ivy review bot” for you.'],
                         observations(self)['page']['agentIntro'])


class TheInvitePageForANewPerson(unittest.TestCase):

    def test_a_new_persons_invite_still_takes_a_password_and_redeems(self):
        seen = observations(self)['page']['newPerson']
        self.assertTrue(seen['hasNewPassword'])
        self.assertTrue(seen['hasConfirm'])
        self.assertEqual(0, seen['signInForms'])
        self.assertEqual(0, seen['acceptButtons'])
        self.assertEqual('Olga invites Lena Park to Orchestra, with the username @lena.', seen['firstLine'])
        self.assertEqual([['viewInvite', TOKEN], ['redeemInvite', TOKEN, 'a good long password']], seen['calls'])
        self.assertEqual('Your account is ready', seen['title'])

    def test_the_new_person_pages_agent_line_names_the_agents_role(self):
        # kittrial-5bb.581 r2 (review N4): a reviewer agent is not called a worker agent.
        seen = observations(self)['page']['newPersonAgent']
        self.assertEqual(1, len(seen['reviewer']))
        self.assertTrue(seen['reviewer'][0].startswith('A reviewer agent, “Lena bot”, is made for you in North'), seen)
        self.assertTrue(seen['worker'][0].startswith('A worker agent, “Lena bot”'), seen)


class TheUpgradedBanner(unittest.TestCase):

    def test_an_answer_from_another_build_shows_the_banner_once(self):
        seen = observations(self)['banner']['changed']
        self.assertEqual(0, seen['before'])
        self.assertEqual(1, seen['count'])
        self.assertEqual('status', seen['role'])
        self.assertTrue(seen['text'].startswith('Orchestra was upgraded. Reload.'))
        self.assertEqual(1, seen['reloadButtons'])
        self.assertEqual(1, seen['afterAnotherChange'])
        self.assertEqual(1, seen['reloads'])

    def test_the_same_build_or_an_empty_value_shows_no_banner(self):
        banner = observations(self)['banner']
        self.assertEqual(0, banner['same']['count'])
        self.assertEqual(0, banner['emptyValue']['count'])

    def test_a_tab_made_visible_asks_healthz_at_most_once_a_minute_and_a_new_build_id_shows_the_banner(self):
        seen = observations(self)['banner']['visible']
        self.assertEqual(1, seen['sameHealthCalls'])
        self.assertEqual(0, seen['noBannerYet'])
        self.assertEqual(0, seen['hiddenCalls'])
        self.assertEqual(1, seen['changedHealthCalls'])
        self.assertEqual(1, seen['count'])


if __name__ == '__main__':
    unittest.main()
