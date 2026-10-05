"""kittrial-5bb.90: the four P3s of the kittrial-5bb.84 review.

(1) a superuser PATCHing another person's agent onto an unusable record must be
refused by the same owner-membership rule the service itself uses; (2) a record that
becomes unusable (its creator demoted) must stop being served at once, not when the
short read cache expires; (3) a canonical name that is a literal route word
(``unconfirmed``) cannot be registered; (4) confirming a legacy record backfills
``registered_by``, and a new credential for an agent that already holds an unusable
grant is refused. Reproduces the reviewer's probes in
koopa:/home/james/orchestra-review-evidence/1003-5bb84/.

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
