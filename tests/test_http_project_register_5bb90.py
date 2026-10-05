"""kittrial-5bb.90: the four P3s of the kittrial-5bb.84 review, and the three review
items of the revision-2 request.

(1) a superuser PATCHing another person's agent onto an unusable record must be
refused by the same owner-membership rule the service itself uses, and that rule must
not answer an unauthorised caller (kittrial-5bb.90 item 1); (2) a record that becomes
unusable (its creator demoted) must stop being served at once, not when the short read
cache expires; (3) a canonical name that is a literal route word (``unconfirmed``)
cannot be registered, whatever records already exist; (4) confirming a legacy record
backfills ``registered_by``, and a new credential for an agent that already holds an
unusable grant is refused - unless the agent is disabled (the service's own earlier
refusal answers first) or the record is archived (whose refusal names the route that
works, kittrial-5bb.90 item 2). Reproduces the reviewer's probes in
koopa:/home/james/orchestra-review-evidence/1003-5bb84/ and
koopa:/home/james/orchestra-review-evidence/1005-5bb90/.

Kept in its own module so the pre-existing tests in test_http_project_register.py,
and their timing, are untouched.
"""
import sys
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_service
from test_http_project_register import RegisterHarness


class Fivebb90Case(RegisterHarness):
    def legacy(self, project_id, creator, initialized=True, **extra):
        """A record as the kit before kittrial-5bb.80 left it (the .84 review fixture)."""
        if initialized:
            self.initialize_canonical(project_id)
        with self.service.store.lock:
            self.service.state['projects'][project_id] = dict({
                'id': project_id, 'name': 'Legacy ' + project_id, 'created_by': creator,
                'created_at': '2026-09-01T00:00:00Z', 'archived': False}, **extra)
            self.service.state['memberships'][project_id] = {creator: 'owner'}
            self.service.store.save()

    def user_id(self, token):
        return self.request('GET', '/v1/sessions/current', token=token).data['user']['id']

    def account(self, name):
        """A second ordinary account, logged in."""
        admin = self.admin_token()
        self.create_account(admin, name, name + '-password-1')
        return self.login(name, name + '-password-1')[0]

    def superuser(self, user_id, value=True):
        with self.service.store.lock:
            self.service.state['users'][user_id]['superuser'] = value
            self.service.store.save()

    def agent_holding_unusable_grant(self, token, name, project_id, directory):
        """An agent whose grant is on a record that has since become unusable.

        A grant can only be made while the record is usable, so the record is created
        confirmed and the superuser mark is removed afterwards: exactly the state an
        upgrade from the .80 kit leaves behind (the .84 review's fixture).
        """
        agent = self.request('POST', '/v1/agents', {'name': name, 'working_directory': directory,
                                                    'projects': [project_id]}, token=token)
        self.assertEqual(201, agent.status, agent.data)
        agent_id = agent.data['agent']['id']
        with self.service.store.lock:
            self.service.state['projects'][project_id].pop('confirmed_by', None)
            self.service.store.save()
        return agent_id

    # -- kittrial-5bb.84 item 1 (and kittrial-5bb.90 items 1 and 3) ------------
    def test_a_superuser_cannot_grant_an_unusable_project_on_someone_elses_agent(self):
        """(1) `_require_grantable` judged the *caller's* memberships; the agent owner's are
        what the service's own grant rule uses."""
        admin, alex = self.alex()
        alex_id = self.user_id(alex)
        self.legacy('legacy', alex_id)                          # alex owns it; admin is not a member
        agent = self.request('POST', '/v1/agents', {'name': 'Falcon',
                                                    'working_directory': '/home/alex/f'}, token=alex)
        self.assertEqual(201, agent.status, agent.data)
        agent_id = agent.data['agent']['id']
        self.assertNotIn(self.admin_user['id'], self.service.state['memberships']['legacy'])
        granted = self.request('PATCH', '/v1/agents/%s' % agent_id, {'projects': ['legacy']},
                               token=admin)
        self.assertEqual(409, granted.status, granted.data)
        self.assertIn('no superuser has confirmed it', granted.data['error']['message'])
        self.assertEqual([], self.service.state['agents'][agent_id]['projects'])
        # The same grant by the owner is refused too, and a project the owner cannot see is
        # still the service's own 404 (the owner rule the refusal must not bypass).
        self.assertEqual(409, self.request('PATCH', '/v1/agents/%s' % agent_id,
                                           {'projects': ['legacy']}, token=alex).status)
        with self.service.store.lock:
            self.service.state['projects']['outsider'] = {
                'id': 'outsider', 'name': 'Outsider', 'created_by': self.admin_user['id'],
                'created_at': '2026-09-01T00:00:00Z', 'archived': False, 'registered_by': True}
            self.service.state['memberships']['outsider'] = {self.admin_user['id']: 'owner'}
            self.service.store.save()
        unseen = self.request('PATCH', '/v1/agents/%s' % agent_id, {'projects': ['outsider']},
                              token=admin)
        self.assertEqual(404, unseen.status, unseen.data)

    def test_the_owners_membership_not_the_callers_decides_the_grant(self):
        """(item 1) The guard reads the AGENT OWNER's membership: a superuser caller who is
        not a member of the record must not make it fire, and the service's own
        "Project not found" 404 is then the answer. Judging the caller again answers 409
        (mutation M1), and not passing the owner at all does the same (M4)."""
        admin, alex = self.alex()
        alex_id = self.user_id(alex)
        carol_id = self.user_id(self.account('carol'))
        self.legacy('danas', carol_id)           # unusable; alex and admin are not members
        agent = self.request('POST', '/v1/agents', {'name': 'Harrier',
                                                    'working_directory': '/home/alex/h'}, token=alex)
        agent_id = agent.data['agent']['id']
        self.assertNotIn(alex_id, self.service.state['memberships']['danas'])
        unseen = self.request('PATCH', '/v1/agents/%s' % agent_id, {'projects': ['danas']},
                              token=admin)
        self.assertEqual(404, unseen.status, unseen.data)
        self.assertEqual('not_found', unseen.data['error']['code'])
        self.assertEqual([], self.service.state['agents'][agent_id]['projects'])

    def test_an_owner_who_is_a_superuser_is_judged_too(self):
        """(item 1) The superuser flag that matters belongs to the OWNER: the record's
        usability is still judged when the owner is a superuser granting their own agent.
        Not judging a superuser owner lets the grant through (mutation M2)."""
        admin, alex = self.alex()
        alex_id = self.user_id(alex)
        carol_id = self.user_id(self.account('carol'))
        self.legacy('danas', carol_id)           # unusable; not a record alex is a member of
        self.superuser(alex_id)                  # ... but the AGENT OWNER is a superuser
        agent = self.request('POST', '/v1/agents', {'name': 'Merlin',
                                                    'working_directory': '/home/alex/m'}, token=alex)
        agent_id = agent.data['agent']['id']
        refused = self.request('PATCH', '/v1/agents/%s' % agent_id, {'projects': ['danas']},
                               token=alex)
        self.assertEqual(409, refused.status, refused.data)
        self.assertIn('no superuser has confirmed it', refused.data['error']['message'])
        self.assertEqual([], self.service.state['agents'][agent_id]['projects'])

    def test_an_unauthorised_caller_gets_the_agent_404_not_the_record_409(self):
        """(item 1) bob may not touch alex's agent. Judging the owner before the service
        has checked that is a refusal bob must not receive: it tells a caller with no
        rights that the agent id exists, that the record exists and that its owner is a
        member. Removing the caller-authority gate (mutation M12) answers 409 here."""
        admin, alex = self.alex()
        alex_id = self.user_id(alex)
        bob = self.account('bob')
        bob_id = self.user_id(bob)
        self.legacy('legacy', alex_id)
        with self.service.store.lock:            # bob IS a member: a caller-scoped judgment fires
            self.service.state['memberships']['legacy'][bob_id] = 'contributor'
            self.service.store.save()
        agent = self.request('POST', '/v1/agents', {'name': 'Falcon',
                                                    'working_directory': '/home/alex/f'}, token=alex)
        agent_id = agent.data['agent']['id']
        denied = self.request('PATCH', '/v1/agents/%s' % agent_id, {'projects': ['legacy']},
                              token=bob)
        self.assertEqual(404, denied.status, denied.data)
        self.assertEqual('Agent not found', denied.data['error']['message'])
        self.assertNotIn('no superuser has confirmed it', denied.data['error']['message'])
        self.assertEqual([], self.service.state['agents'][agent_id]['projects'])

    def test_an_unauthorised_caller_gets_no_record_409_for_a_superusers_agent(self):
        """(item 1) With a superuser owner the old guard fired for ANY unusable record id,
        including one neither the caller nor the owner belongs to, and 404 only for an id
        with no record; that difference is the probe. Both must be the agent 404 now."""
        admin, alex = self.alex()
        bob = self.account('bob')
        carol_id = self.user_id(self.account('carol'))
        self.legacy('legacy', carol_id)          # unusable; neither bob nor admin is a member
        agent = self.request('POST', '/v1/agents', {'name': 'Raven',
                                                    'working_directory': '/home/root/r'}, token=admin)
        agent_id = agent.data['agent']['id']
        for project_id in ('legacy', 'november'):
            denied = self.request('PATCH', '/v1/agents/%s' % agent_id, {'projects': [project_id]},
                                  token=bob)
            self.assertEqual(404, denied.status, denied.data)
            self.assertEqual('Agent not found', denied.data['error']['message'])
        self.assertEqual([], self.service.state['agents'][agent_id]['projects'])

    def test_a_superuser_may_still_edit_an_agent_that_already_holds_the_unusable_grant(self):
        admin, alex = self.alex()
        alex_id = self.user_id(alex)
        self.legacy('legacy', alex_id, confirmed_by=self.admin_user['id'])
        agent = self.request('POST', '/v1/agents', {'name': 'Kestrel',
                                                    'working_directory': '/home/alex/k',
                                                    'projects': ['legacy']}, token=alex)
        agent_id = agent.data['agent']['id']
        with self.service.store.lock:                            # ... then as the upgrade finds it
            del self.service.state['projects']['legacy']['confirmed_by']
        kept = self.request('PATCH', '/v1/agents/%s' % agent_id,
                            {'projects': ['legacy'], 'notes': 'held'}, token=admin)
        self.assertEqual(200, kept.status, kept.data)
        self.assertEqual('held', self.service.state['agents'][agent_id]['notes'])

    # -- kittrial-5bb.84 item 4 (and kittrial-5bb.90 items 2 and 3) ------------
    def test_a_new_credential_is_refused_while_the_agent_holds_an_unusable_grant(self):
        """(4, last sentence) A new credential extends the agent's access to the record."""
        admin, alex = self.alex()
        alex_id = self.user_id(alex)
        self.legacy('legacy', alex_id, confirmed_by=self.admin_user['id'])
        agent = self.request('POST', '/v1/agents', {'name': 'Osprey',
                                                    'working_directory': '/home/alex/o',
                                                    'projects': ['legacy']}, token=alex)
        agent_id = agent.data['agent']['id']
        with self.service.store.lock:
            del self.service.state['projects']['legacy']['confirmed_by']
        for token in (alex, admin):
            issued = self.request('POST', '/v1/agents/%s/credentials' % agent_id,
                                  {'label': 'second'}, token=token)
            self.assertEqual(409, issued.status, issued.data)
            self.assertIn('nothing that grants access is accepted', issued.data['error']['message'])
        self.assertEqual(1, len(self.service.state['agents'][agent_id]['projects']))

    def test_a_new_credential_for_an_unauthorised_caller_is_the_agent_404(self):
        """(item 3.1) The credential refusal must be given only to a caller who may
        administer the agent. Giving it to any caller (mutation M10) answers 409 to bob."""
        admin, alex = self.alex()
        alex_id = self.user_id(alex)
        bob = self.account('bob')
        self.legacy('legacy', alex_id, confirmed_by=self.admin_user['id'])
        self.legacy('legacyroot', self.admin_user['id'], confirmed_by=self.admin_user['id'])
        agents = (self.agent_holding_unusable_grant(alex, 'Osprey', 'legacy', '/home/alex/o'),
                  self.agent_holding_unusable_grant(admin, 'Rook', 'legacyroot', '/home/root/r'))
        for agent_id in agents:
            denied = self.request('POST', '/v1/agents/%s/credentials' % agent_id,
                                  {'label': 'x'}, token=bob)
            self.assertEqual(404, denied.status, denied.data)
            self.assertEqual('Agent not found', denied.data['error']['message'])
            self.assertNotIn('no superuser has confirmed it', denied.data['error']['message'])

    def test_a_disabled_agent_hears_the_disabled_refusal_first(self):
        """(item 3.2) The service's own earlier disabled check must answer, not the
        unusable-record sentence the route's new check used to give first."""
        admin, alex = self.alex()
        alex_id = self.user_id(alex)
        self.legacy('legacy', alex_id, confirmed_by=self.admin_user['id'])
        agent_id = self.agent_holding_unusable_grant(alex, 'Saker', 'legacy', '/home/alex/s')
        self.assertEqual(200, self.request('POST', '/v1/agents/%s/disable' % agent_id, {},
                                           token=alex).status)
        for token in (alex, admin):
            refused = self.request('POST', '/v1/agents/%s/credentials' % agent_id,
                                   {'label': 'x'}, token=token)
            self.assertEqual(409, refused.status, refused.data)
            self.assertEqual('A disabled agent cannot receive a credential',
                             refused.data['error']['message'])

    def test_an_archived_unusable_record_names_the_route_that_works(self):
        """(item 2.1) An archived record cannot be confirmed, so the refusal must not send
        the owner to confirm or archive it. Removing the grant is named, and it works."""
        admin, alex = self.alex()
        alex_id = self.user_id(alex)
        self.legacy('legacy', alex_id, confirmed_by=self.admin_user['id'])
        agent_id = self.agent_holding_unusable_grant(alex, 'Kite', 'legacy', '/home/alex/k')
        self.assertEqual(200, self.request('POST', '/v1/projects/legacy/archive', {},
                                           token=alex).status)
        refused = self.request('POST', '/v1/agents/%s/credentials' % agent_id,
                               {'label': 'again'}, token=alex)
        self.assertEqual(409, refused.status, refused.data)
        message = refused.data['error']['message']
        self.assertIn('is archived', message)
        self.assertIn('cannot be confirmed', message)
        self.assertNotIn('A superuser confirms it', message)
        # The named route: remove the grant, then the rest of the agent works again.
        kept = self.request('PATCH', '/v1/agents/%s' % agent_id, {'projects': []}, token=alex)
        self.assertEqual(200, kept.status, kept.data)
        self.assertEqual([], self.service.state['agents'][agent_id]['projects'])
        issued = self.request('POST', '/v1/agents/%s/credentials' % agent_id,
                              {'label': 'again'}, token=alex)
        self.assertEqual(201, issued.status, issued.data)

    # -- kittrial-5bb.84 item 2 -----------------------------------------------
    def test_the_task_list_is_not_served_from_cache_after_the_creator_is_demoted(self):
        """(2) The reviewer saw the cached list answer 200 for the full 20 s."""
        admin, alex = self.alex()
        alex_id = self.user_id(alex)
        self.legacy('legacy', alex_id)                          # usable only while its creator is a superuser
        with self.service.store.lock:
            self.service.state['users'][alex_id]['superuser'] = True
            self.service.store.save()
        alex = self.login('alex', 'alex-password-1')[0]
        first = self.request('GET', '/v1/projects/legacy/tasks', token=alex)
        self.assertEqual(200, first.status, first.data)         # caches the snapshot for 20 s
        with self.service.store.lock:
            self.service.state['users'][alex_id]['superuser'] = False
            self.service.store.save()
        again = self.request('GET', '/v1/projects/legacy/tasks', token=alex)
        self.assertEqual(409, again.status, again.data)         # not the cached 200
        self.assertIn('no superuser has confirmed it', again.data['error']['message'])
        # Confirming it is served at once too, whatever the previous verdict was.
        self.assertEqual(200, self.request('POST', '/v1/projects/legacy/confirm', {}, token=admin).status)
        self.assertEqual(200, self.request('GET', '/v1/projects/legacy/tasks', token=alex).status)

    # -- kittrial-5bb.84 item 3 -----------------------------------------------
    def test_a_reserved_route_name_cannot_be_registered(self):
        """(3) A canonical project named `unconfirmed` shadowed the upgrade-check route."""
        admin, _ = self.alex()
        self.initialize_canonical('unconfirmed')
        self.backend.projects = []
        refused = self.register(admin, {'project_id': 'unconfirmed'})
        self.assertEqual(422, refused.status, refused.data)
        self.assertIn('reserved', refused.data['error']['message'])
        self.assertEqual(self.backend.projects, [])              # refused before the host read
        self.assertNotIn('unconfirmed', self.service.state['projects'])
        review = self.request('GET', '/v1/projects/unconfirmed', token=admin)
        self.assertEqual(200, review.status, review.data)        # still the review list
        self.assertIn('items', review.data)
        self.initialize_canonical('gamma')
        self.assertEqual(201, self.register(admin, {'project_id': 'gamma'}).status)

    def test_the_reserved_name_wins_over_an_existing_record(self):
        """(item 3.1) The reservation is checked before the existing-record check, so a
        record already under the reserved name cannot turn the refusal into an "already
        registered" 409 that hides it. Moving the check after it does (mutation M11)."""
        admin, alex = self.alex()
        alex_id = self.user_id(alex)
        self.legacy('unconfirmed', alex_id)                      # the name is already taken
        self.backend.projects = []
        refused = self.register(admin, {'project_id': 'unconfirmed'})
        self.assertEqual(422, refused.status, refused.data)
        self.assertIn('reserved', refused.data['error']['message'])
        self.assertEqual(self.backend.projects, [])              # refused before the host read

    # -- kittrial-5bb.84 item 4, backfill --------------------------------------
    def test_confirming_a_legacy_record_backfills_the_registered_by_mark(self):
        """(4) A record a superuser confirms is marked as superuser-backed itself."""
        admin, alex = self.alex()
        alex_id = self.user_id(alex)
        self.legacy('legacy', alex_id)
        done = self.request('POST', '/v1/projects/legacy/confirm', {}, token=admin)
        self.assertEqual(200, done.status, done.data)
        record = self.service.state['projects']['legacy']
        self.assertEqual((record['registered_by'], record['confirmed_by']),
                         (self.admin_user['id'], self.admin_user['id']))
        # The backfilled mark alone keeps it usable: demote the confirmer and drop the
        # confirmation, as a stricter upgrade would.
        with self.service.store.lock:
            self.service.state['users'][self.admin_user['id']]['superuser'] = False
            del record['confirmed_by']
            self.service.store.save()
        self.assertIsNone(http_service.project_unusable(self.service, 'legacy'))


if __name__ == '__main__':
    unittest.main()
