"""A project's coordinator writes and accepts requirements on the web (kittrial-5bb.278).

Slice 10 of docs/WEB_COORDINATOR_DESIGN.md. A person with the Coordinator role, and an
agent holding that project's coordinator grant, do everything to requirements the owner
does - create, revise, accept (self-acceptance included), withdraw, supersede, finish or
abandon a recovery, read the BRD, and accept many at once. Everything else about the
project still refuses them, and the accepting party in every record stays a HUMAN account.

Through the real routes of the web service and the shipped canonical adapter, with the
native tracker modeled by ``OwnerNative``. The pinned real-bd module is separate.
"""
import json
import secrets
import shutil
import sys
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import requirement_http as requirements
import requirement_owner_records as owner_records
import test_http_requirements_owner as owner_fixture
from http_authority import REQUIREMENT_EDITOR_REFUSED


class CoordinatorTests(owner_fixture.OwnerHttpTests):
    """alex owns Alpha; casey is its coordinator; ``Heron`` is alex's granted agent."""

    def setUp(self):
        super().setUp()
        admin = self.admin_token()
        self.admin_id = self.service.authenticate(admin).user_id
        # A person with the Coordinator role: approved by an owner, no administration.
        self.coordinator_id = self.create_account(admin, 'casey', 'casey-password-1')
        self.coordinator_token = self.login('casey', 'casey-password-1')[0]
        self.assertEqual(200, self.request('PUT', self.base + '/members/' + self.coordinator_id,
                                          {'role': 'coordinator'}, token=self.owner_token).status)
        # A contributor: a member, so the refusal is about the role and not about the project.
        self.contributor_id = self.create_account(admin, 'dana', 'dana-password-1')
        self.contributor_token = self.login('dana', 'dana-password-1')[0]
        self.assertEqual(200, self.request('PUT', self.base + '/members/' + self.contributor_id,
                                          {'role': 'contributor'}, token=self.owner_token).status)
        # A personal agent of the owner, in this project, with its coordinator grant.
        made = self.request('POST', '/v1/agents', {
            'name': 'Heron', 'working_directory': '/tmp/synthetic-heron',
            'projects': [self.project]}, token=self.owner_token)
        self.assertEqual(201, made.status, made.data)
        self.agent_id = made.data['agent']['id']
        self.agent_secret = made.data['credential']['secret']
        self.agent_credential_id = made.data['credential']['id']
        self.assertEqual(200, self.request('PUT', self.base + '/agents/%s/coordinator' % self.agent_id,
                                          {}, token=self.owner_token).status)
        # A second agent with no grant, and a worker credential: both are refused.
        plain = self.request('POST', '/v1/agents', {
            'name': 'Plover', 'working_directory': '/tmp/synthetic-plover',
            'projects': [self.project]}, token=self.owner_token)
        self.assertEqual(201, plain.status, plain.data)
        self.plain_agent_secret = plain.data['credential']['secret']
        self.worker_secret = self.issue_credential(
            self.owner_token, self.project, label='worker', scopes=['read', 'tasks'])['secret']

    # -- fixtures -------------------------------------------------------------
    def create(self, token=None, title='A requirement', key=None):
        result = self.request('POST', self.base + '/requirements', {
            'kind': 'requirement', 'parent': 'job-1', 'title': title,
            'description': 'Content written on the site.'},
            token=token or self.owner_token, key=key or 'create-' + secrets.token_hex(6))
        self.assertEqual(result.status, 201, result.data)
        return result.data

    def accept(self, item, token=None, key=None):
        return self.request('POST', self.base + '/requirements/%s/accept' % item['id'],
                            self.expected(item), token=token or self.owner_token,
                            key=key or 'accept-' + secrets.token_hex(6))

    def audit_events(self, action):
        return [event for event in self.service.state['audit']
                if event['action'] == action and event['outcome'] == 'committed']

    def revoke_grant(self):
        grants = self.service.state.get('coordinator_grants') or {}
        grants.get(self.project, {}).pop(self.agent_id, None)
        self.service.store.save()

    def arm_before_call(self, number, hook):
        """Run ``hook`` immediately before this request's endpoint call ``number``.

        The backend calls the hook once, just before the canonical adapter, which is
        exactly the window between the web service's own check and the effect.
        """
        seen = {'calls': 0}

        def armed():
            seen['calls'] += 1
            if seen['calls'] < number:
                self.backend.before_owner = armed
                return
            hook()
        self.backend.before_owner = armed

    # -- who may edit ---------------------------------------------------------
    def test_the_coordinator_does_what_the_owner_does_and_self_acceptance_is_recorded(self):
        first = self.create(token=self.coordinator_token, title='Coordinator draft')
        branch = self.base + '/requirements/' + first['id']
        # The page and the document are the coordinator's, exactly as the owner's are.
        listing = self.request('GET', self.base + '/requirements', token=self.coordinator_token)
        self.assertEqual(200, listing.status, listing.data)
        self.assertTrue(listing.data['can_edit'])
        detail = self.request('GET', branch, token=self.coordinator_token)
        self.assertEqual(200, detail.status, detail.data)
        self.assertTrue(detail.data['can_edit'])
        # Revise, then accept the revision the coordinator proposed itself (owner decision
        # 01a13b6f: requirement acceptance is exempt from the slice-1a party rule).
        revised = self.request('PATCH', branch, dict(self.expected(first), description='Revised on the site'),
                               token=self.coordinator_token, key='coordinator-revise')
        self.assertEqual(200, revised.status, revised.data)
        accepted = self.request('POST', branch + '/accept', self.expected(revised.data),
                                token=self.coordinator_token, key='coordinator-accept')
        self.assertEqual(200, accepted.status, accepted.data)
        self.assertEqual(accepted.data['acceptance_state'], 'accepted')
        row = self.backend.native.row(first['id'])
        evidence = owner_records.existing_acceptances(row)[accepted.data['revision']]
        self.assertEqual(evidence['account_id'], self.coordinator_id)
        self.assertEqual(evidence['decision']['owners'], [self.coordinator_id])
        # The BRD read and a withdrawal of a different requirement both work for the
        # coordinator, and the audit names the person, never an agent.
        second = self.create(title='Withdrawn later')
        accepted_second = self.accept(second).data
        brd = self.request('GET', self.base + '/brd', token=self.coordinator_token)
        self.assertEqual(200, brd.status, brd.data)
        withdrawn = self.request('POST', self.base + '/requirements/%s/withdraw' % second['id'],
                                 dict(self.expected(accepted_second), expected_state_sha256=None,
                                      reason='Replaced by the coordinator draft.'),
                                 token=self.coordinator_token, key='coordinator-withdraw')
        self.assertEqual(200, withdrawn.status, withdrawn.data)
        for event in self.audit_events('requirements.accept'):
            self.assertIsNone(event.get('acting_agent'), event)
        # The governance switch changes the rules, not the text: still the owner's alone.
        mode = self.request('GET', self.base + '/requirements/governance',
                            token=self.owner_token).data
        refused = self.request('PUT', self.base + '/requirements/governance',
                               dict(self.expected(mode), mode='governed'),
                               token=self.coordinator_token, key='coordinator-mode')
        self.assertEqual(403, refused.status, refused.data)
        self.assertIn('needs the signed-in project owner', refused.data['error']['message'])
        # The adapter refuses it too, even when reached directly with the coordinator's own
        # descriptor: the rule is checked at effect time, not only at the route.
        from http_authority import (AuthorityConfig, CAP_COORDINATE, NativeRunner,
                                    authority_request, run_guarded)
        principal = self.service.authenticate(self.coordinator_token)
        before = len(self.backend.native.writes())
        built = dict(project=self.project, actor=principal.user_id, action='owner-requirements',
                     args=['governance'],
                     attachments={'payload': {'flag': '--file', 'text': json.dumps(
                         dict(self.expected(mode), mode='governed'))}},
                     operation_id='adapter-governance-coordinator',
                     authority=authority_request(principal, self.project, CAP_COORDINATE,
                                                 now=self.service._expiry_now()),
                     route='requirements.governance')

        def guarded(root, request, journal, effect, **options):
            return run_guarded(request, journal, effect, **options)
        with self.assertRaisesRegex(ValueError, 'needs the signed-in project owner'):
            requirements.web_action(Path(self.canonical_root), self.path, self.project, built,
                                    AuthorityConfig(str(self.service.store.path)),
                                    NativeRunner(self.backend.native), guarded)
        self.assertEqual(len(self.backend.native.writes()), before)
        unchanged = self.request('GET', self.base + '/requirements/governance',
                                 token=self.owner_token).data
        self.assertEqual(unchanged['mode'], 'simple')

    def test_the_agents_acceptance_stays_the_human_account_and_the_grant_is_audited(self):
        first = self.create(token=self.agent_secret, title='Agent draft')
        accepted = self.accept(first, token=self.agent_secret, key='agent-accept')
        self.assertEqual(200, accepted.status, accepted.data)
        row = self.backend.native.row(first['id'])
        evidence = owner_records.existing_acceptances(row)[accepted.data['revision']]
        # The party is the human account that owns the agent: the record schema is unchanged.
        self.assertEqual(evidence['account_id'], self.account)
        self.assertEqual(evidence['decision']['owners'], [self.account])
        events = [event for event in self.audit_events('requirements.accept')
                  if event.get('credential_id') == self.agent_credential_id]
        self.assertEqual(len(events), 1, self.service.state['audit'][-5:])
        event = events[0]
        self.assertEqual(event['user_id'], self.account)
        self.assertEqual(event['acting_agent'], self.agent_id)
        self.assertEqual(event['coordinator_grant']['granted_by'], self.account)
        # And the agent reads and writes the rest of the surface.
        self.assertEqual(200, self.request('GET', self.base + '/brd',
                                           token=self.agent_secret).status)
        second = self.create(token=self.agent_secret, title='Agent second')
        self.assertEqual(200, self.accept(second, token=self.agent_secret,
                                          key='agent-accept-2').status)
        listed = self.request('GET', self.base + '/requirements', token=self.agent_secret)
        self.assertEqual(200, listed.status, listed.data)
        self.assertFalse(listed.data['can_edit'], 'an agent uses the routes, not the page')

    def test_refusals_name_the_rule_and_write_nothing(self):
        first = self.create()
        before = len(self.backend.native.writes())
        for label, token in (('contributor', self.contributor_token),
                             ('worker credential', self.worker_secret),
                             ('ungranted agent', self.plain_agent_secret)):
            with self.subTest(principal=label):
                refused = self.request('POST', self.base + '/requirements/%s/accept' % first['id'],
                                       self.expected(first), token=token, key='denied-' + secrets.token_hex(6))
                self.assertEqual(403, refused.status, refused.data)
                self.assertEqual(refused.data['error']['message'], REQUIREMENT_EDITOR_REFUSED)
                created = self.request('POST', self.base + '/requirements', {
                    'kind': 'requirement', 'parent': 'job-1', 'title': 'Should not exist',
                    'description': 'Refused.'}, token=token, key='denied-create-' + secrets.token_hex(6))
                self.assertEqual(403, created.status, created.data)
        # A person who coordinates ANOTHER project is refused here, and learns nothing new
        # about this one: the membership answer is the service's own 404, as it was before
        # this slice.
        admin = self.admin_token()
        rita = self.create_account(admin, 'rita', 'rita-password-1')
        rita_token = self.login('rita', 'rita-password-1')[0]
        beta = self.create_project(admin, 'Beta')
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s'
                                          % (beta, rita),
                                          {'role': 'coordinator'}, token=admin).status)
        outsider = self.request('POST', self.base + '/requirements/%s/accept' % first['id'],
                               self.expected(first), token=rita_token, key='outsider-accept')
        self.assertEqual(404, outsider.status, outsider.data)
        self.assertEqual(len(self.backend.native.writes()), before)

    def test_the_coordinator_route_opens_no_other_administration(self):
        for method, path, body in (
                ('PUT', '/members/%s' % self.contributor_id, {'role': 'viewer'}),
                ('POST', '/worker-credentials', {'label': 'x'}),
                ('GET', '/audit', None), ('GET', '/agents', None), ('POST', '/archive', {}),
                ('GET', '/setup', None),
                ('PUT', '/agents/%s/coordinator' % self.agent_id, {})):
            with self.subTest(path=path):
                refused = self.request(method, self.base + path, body,
                                       token=self.coordinator_token)
                self.assertEqual(403, refused.status, (path, refused.data))

    def test_a_demoted_coordinator_and_a_removed_grant_write_nothing_at_effect_time(self):
        first = self.create()
        before = len(self.backend.native.writes())

        def demote():
            self.service.state['memberships'][self.project][self.coordinator_id] = 'contributor'
            self.service.store.save()

        self.backend.before_owner = demote
        demoted = self.request('POST', self.base + '/requirements/%s/accept' % first['id'],
                               self.expected(first), token=self.coordinator_token,
                               key='demoted-coordinator')
        self.assertIn(demoted.status, (403, 422), demoted.data)
        self.assertEqual(len(self.backend.native.writes()), before)
        # The same for an agent whose grant is removed between the check and the effect.
        self.backend.native.actor = self.account
        second = self.create()
        before = len(self.backend.native.writes())
        self.backend.before_owner = self.revoke_grant
        removed = self.request('POST', self.base + '/requirements/%s/accept' % second['id'],
                               self.expected(second), token=self.agent_secret,
                               key='removed-grant')
        self.assertIn(removed.status, (403, 422), removed.data)
        self.assertEqual(len(self.backend.native.writes()), before)

    # -- the two surviving mutants, and the superuser answer (kittrial-5bb.318) -----
    def test_a_granted_agent_is_refused_once_its_account_loses_coordinate(self):
        """Mutant Q2 (kittrial-5bb.318 item 1): the agent branch's own coordinate check.

        The web route refuses this request anyway: ``http_authority.decide`` reads the
        issuer's role when it decides the credential's ``coordinate``, and the route
        reports every 403 with the same sentence. A pin that only went through the route
        would therefore pass with the check deleted - which is how the mutant survived.
        The second half calls the canonical adapter DIRECTLY with the agent's own live
        descriptor: nothing in front of ``requirement_editor`` decides the capability
        there, so only that check can stand between this request and the write.
        """
        first = self.create()                 # a draft: an accepted request would write
        # alex - the owner, and this agent's own human account - keeps the agent's grant
        # and loses the role. The account is a contributor now; the agent is not an editor.
        self.service.state['memberships'][self.project][self.account] = 'contributor'
        self.service.store.save()
        before = len(self.backend.native.writes())
        refused = self.accept(first, token=self.agent_secret, key='demoted-issuer-route')
        self.assertEqual(403, refused.status, refused.data)
        self.assertEqual(REQUIREMENT_EDITOR_REFUSED, refused.data['error']['message'])
        # The adapter, reached directly with the agent's own descriptor.
        from http_authority import (AuthorityConfig, CAP_COORDINATE, NativeRunner,
                                    authority_request, run_guarded)
        principal = self.service.authenticate(self.agent_secret)
        built = dict(project=self.project, actor=principal.user_id, action='owner-requirements',
                     args=['accept', first['id']],
                     attachments={'payload': {'flag': '--file',
                                              'text': json.dumps(self.expected(first))}},
                     operation_id='adapter-demoted-issuer-agent',
                     authority=authority_request(principal, self.project, CAP_COORDINATE,
                                                 now=self.service._expiry_now()),
                     route='requirements.accept')

        def guarded(root, request, journal, effect, **options):
            return run_guarded(request, journal, effect, **options)
        with self.assertRaisesRegex(ValueError, 'needs its owner or a coordinator'):
            requirements.web_action(Path(self.canonical_root), self.path, self.project, built,
                                    AuthorityConfig(str(self.service.store.path)),
                                    NativeRunner(self.backend.native), guarded)
        self.assertEqual(len(self.backend.native.writes()), before)

    def test_the_credential_service_allowlist_is_exactly_owner_requirements(self):
        """Mutant Q6 (kittrial-5bb.318 item 2): the credential allowlist is pinned.

        ``project_creation.CREDENTIAL_SERVICE_ACTIONS`` is what lets an agent's credential
        reach a service action at all. Widening it (here: adding ``set-onboarding``) was
        refused only by that other action's own ``project.admin`` decision, so nothing in
        the suite noticed the widening. Both halves below fail when the set grows: the
        constant itself, and the descriptor check that reads it.
        """
        import project_creation
        from http_authority import AuthorityConfig, CAP_COORDINATE, authority_request
        self.assertEqual(frozenset({'owner-requirements'}),
                         project_creation.CREDENTIAL_SERVICE_ACTIONS)
        config = AuthorityConfig(str(self.service.store.path))
        principal = self.service.authenticate(self.agent_secret)
        for action, capability in sorted(project_creation.SERVICE_ACTIONS.items()):
            with self.subTest(action=action):
                request = {'actor': principal.user_id,
                           'authority': authority_request(principal, self.project, capability,
                                                          now=self.service._expiry_now())}
                if action == 'owner-requirements':
                    # The one action a granted agent's credential may carry.
                    self.assertEqual(principal.user_id,
                                     project_creation.service_descriptor(request, config, action)['user_id'])
                else:
                    with self.assertRaisesRegex(ValueError, 'needs a session descriptor for'):
                        project_creation.service_descriptor(request, config, action)

    def test_a_non_member_superusers_agent_is_refused_like_the_superusers_own_session(self):
        """kittrial-5bb.318 item 3: refuse both, and say why.

        The requirements editor rule is the project's own membership. A superuser who is
        not a member keeps the project's own answer on their session, and before this item
        their granted agent was admitted, because the agent branch read the superuser flag
        as the owner role. Both are refused now with the same sentence: superuser is
        accounts administration, not membership of this project, and admitting the pair
        would widen slice 10's membership rule. A superuser who IS a member (owner here)
        keeps editing through their agent.
        """
        first = self.create()
        admin = self.admin_token()
        admin_id = self.service.authenticate(admin).user_id
        self.service.state['memberships'][self.project].pop(admin_id)   # not a member
        self.service.store.save()
        made = self.request('POST', '/v1/agents', {
            'name': 'Superuser helper', 'working_directory': '/tmp/synthetic-superuser-helper',
            'projects': [self.project]}, token=admin)
        self.assertEqual(201, made.status, made.data)
        helper_secret = made.data['credential']['secret']
        granted = self.request('PUT', self.base + '/agents/%s/coordinator' % made.data['agent']['id'],
                               {}, token=admin)
        self.assertEqual(200, granted.status, granted.data)
        before = len(self.backend.native.writes())
        for label, token in (('the superuser session', admin),
                             ('the superuser agent', helper_secret)):
            with self.subTest(principal=label):
                refused = self.request('POST', self.base + '/requirements/%s/accept' % first['id'],
                                       self.expected(first), token=token,
                                       key='superuser-' + secrets.token_hex(4))
                self.assertEqual(403, refused.status, refused.data)
                self.assertEqual(REQUIREMENT_EDITOR_REFUSED, refused.data['error']['message'])
        self.assertEqual(len(self.backend.native.writes()), before)
        # A superuser who owns this project is a member, so their agent still edits: the
        # rule is the membership, not the flag.
        self.service.state['memberships'][self.project][admin_id] = 'owner'
        self.service.store.save()
        accepted = self.accept(first, token=helper_secret, key='superuser-owner-agent-accept')
        self.assertEqual(200, accepted.status, accepted.data)
        evidence = owner_records.existing_acceptances(
            self.backend.native.row(first['id']))[accepted.data['revision']]
        self.assertEqual(evidence['account_id'], admin_id)

    # -- batch acceptance -----------------------------------------------------
    def batch(self, items, token=None, key=None, stop=None):
        body = {'items': items}
        if stop is not None:
            body['stop_on_error'] = stop
        return self.request('POST', self.base + '/requirements/accept-batch', body,
                            token=token or self.owner_token,
                            key=key or 'batch-' + secrets.token_hex(6))

    @staticmethod
    def item(record):
        return {'id': record['id'], 'expected_revision': record['revision'],
                'expected_sha256': record['sha256']}

    def test_one_action_accepts_many_and_only_the_exact_replay_adds_nothing(self):
        first, second = self.create(title='One'), self.create(title='Two')
        body = [self.item(first), self.item(second)]
        answered = self.batch(body, token=self.coordinator_token, key='batch-both')
        self.assertEqual(200, answered.status, answered.data)
        self.assertEqual(answered.data['summary'], {'accepted': 2, 'refused': 0})
        self.assertEqual([entry['acceptance_state'] for entry in answered.data['accepted']],
                         ['accepted', 'accepted'])
        writes = len(self.backend.native.writes())
        replay = self.batch(body, token=self.coordinator_token, key='batch-both')
        self.assertEqual(replay.status, 200, replay.data)
        self.assertEqual(replay.data, answered.data)
        self.assertEqual(len(self.backend.native.writes()), writes)

    def test_a_refused_item_is_named_and_never_rolls_back_the_others(self):
        first, second, third = (self.create(title='One'), self.create(title='Two'),
                                self.create(title='Three'))
        stale = dict(self.item(third), expected_sha256='0' * 64)
        answered = self.batch([self.item(first), stale, self.item(second)])
        self.assertEqual(200, answered.status, answered.data)
        self.assertEqual(answered.data['summary'], {'accepted': 2, 'refused': 1})
        self.assertEqual([entry['id'] for entry in answered.data['accepted']],
                         [first['id'], second['id']])
        self.assertEqual(answered.data['refused'][0]['id'], third['id'])
        self.assertIn('Reload', answered.data['refused'][0]['reason'])
        row = self.backend.native.row(third['id'])
        self.assertEqual(owner_records.existing_acceptances(row), {})

    def test_stop_on_error_names_every_item_it_did_not_attempt(self):
        first, second, third = (self.create(title='One'), self.create(title='Two'),
                                self.create(title='Three'))
        stale = dict(self.item(first), expected_sha256='0' * 64)
        answered = self.batch([stale, self.item(second), self.item(third)], stop=True)
        self.assertEqual(200, answered.status, answered.data)
        self.assertEqual(answered.data['summary'], {'accepted': 0, 'refused': 3})
        reasons = {entry['id']: entry['reason'] for entry in answered.data['refused']}
        self.assertIn('Not attempted', reasons[second['id']])
        self.assertIn('Not attempted', reasons[third['id']])
        self.assertNotIn('Not attempted', reasons[first['id']])

    def test_bad_batches_are_refused_before_any_effect(self):
        first = self.create()
        before = len(self.backend.native.writes())
        cases = {
            'empty': {'items': []},
            'too many': {'items': [self.item(first)] * 201},
            'duplicate': {'items': [self.item(first), self.item(first)]},
            'missing hash': {'items': [{'id': first['id'], 'expected_revision': 1}]},
            'unknown field': {'items': [dict(self.item(first), extra=1)]},
            'bad stop': {'items': [self.item(first)], 'stop_on_error': 'yes'},
            'no items key': {},
        }
        for label, body in cases.items():
            with self.subTest(case=label):
                refused = self.request('POST', self.base + '/requirements/accept-batch', body,
                                       token=self.owner_token, key='bad-' + secrets.token_hex(6))
                self.assertEqual(422, refused.status, (label, refused.data))
        self.assertEqual(len(self.backend.native.writes()), before)

    def test_a_demotion_mid_batch_stops_the_remaining_items(self):
        first, second, third = (self.create(title='One'), self.create(title='Two'),
                                self.create(title='Three'))

        def demote():
            self.service.state['memberships'][self.project][self.coordinator_id] = 'contributor'
            self.service.store.save()

        # The coordinator is demoted between the service's check and the SECOND item's
        # effect: the first item is accepted, and every later one re-reads authority.
        self.arm_before_call(2, demote)
        answered = self.batch([self.item(first), self.item(second), self.item(third)],
                              token=self.coordinator_token)
        self.assertEqual(200, answered.status, answered.data)
        self.assertEqual(answered.data['summary'], {'accepted': 1, 'refused': 2})
        self.assertEqual([entry['id'] for entry in answered.data['refused']],
                         [second['id'], third['id']])
        for entry in answered.data['refused']:
            self.assertEqual(entry['reason'], REQUIREMENT_EDITOR_REFUSED)
        # Authority is re-read for EACH item: the granted agent is refused the same way once
        # its grant goes, and the item before it stays accepted.
        self.backend.native.actor = self.account
        fourth, fifth = self.create(title='Four'), self.create(title='Five')
        self.arm_before_call(2, self.revoke_grant)
        agent_batch = self.batch([self.item(fourth), self.item(fifth)], token=self.agent_secret)
        self.assertEqual(200, agent_batch.status, agent_batch.data)
        self.assertEqual(agent_batch.data['summary'], {'accepted': 1, 'refused': 1})

    def test_http_client_lets_a_granted_agent_read_and_accept_many(self):
        from http_client import Client, HttpApiError
        client = Client('http://127.0.0.1:%d' % self.port).use_credential(self.agent_secret)
        first, second = self.create(title='One'), self.create(title='Two')
        self.assertEqual(len(client.list_requirements(self.project)['items']), 2)
        self.assertTrue(client.brd(self.project)['items'])
        answered = client.accept_requirements(self.project, [self.item(first), self.item(second)],
                                             key='client-batch-1')
        self.assertEqual(answered['summary'], {'accepted': 2, 'refused': 0})
        single = self.create(title='Three')
        again = client.accept_requirements(self.project, [self.item(single)], key='client-batch-2')
        self.assertEqual(again['summary'], {'accepted': 1, 'refused': 0})
        # The client's own refusal surfaces as an error carrying the rule's sentence.
        third = self.create(title='Four')
        with self.assertRaises(HttpApiError) as refused:
            Client('http://127.0.0.1:%d' % self.port).use_credential(
                self.plain_agent_secret).accept_requirement(self.project, third['id'],
                                                            self.expected(third))
        self.assertEqual(403, refused.exception.status)
        self.assertEqual(refused.exception.message, REQUIREMENT_EDITOR_REFUSED)

    def test_the_coordinator_page_accepts_many_in_one_action(self):
        from test_http_web import run_node_module
        node = shutil.which('node')
        if not node:
            self.skipTest('Node.js is required for page behavior checks')
        self.create(title='One')
        self.create(title='Two')
        root = KIT
        done = run_node_module(self, node, 'await import(process.argv[1])',
            (root/'tests'/'web_requirements_coordinator.mjs').as_uri(),
            (root/'web'/'js'/'api.js').as_uri(),
            (root/'web'/'js'/'views'/'owner_requirements.js').as_uri(),
            'http://127.0.0.1:' + str(self.port), self.project, self.coordinator_token)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(all(json.loads(done.stdout).values()), done.stdout)
