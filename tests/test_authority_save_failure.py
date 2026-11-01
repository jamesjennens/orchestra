"""A failed state save never leaves a WIDER authority in force (kittrial-5bb.487, Fable audit .482 F1).

``Store.save`` raises only before its atomic replace, so nothing of the failed save is on disk. A
widening (grant, promotion, credential, re-enable, ...) is taken back out of memory; a narrowing
(revoke, demotion, removal, disable) keeps its effect and is audited ``committed-unsaved``, written
with the next save. Through the real routes of the web service on the in-process backend.
"""
import contextlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import http_auth
import test_coordinator_grant


class SaveFails:
    """Make the next ``count`` saves of the service state fail, as a full disk or a held lock would."""

    def __init__(self, store, error=None, count=1, when=None):
        self.store, self.error, self.count = store, error or OSError(28, 'No space left on device'), count
        self.real = store._write
        # Only a save of a state for which ``when(state)`` holds fails: the save of the change itself,
        # not a last-use stamp written just before it (which save_soon would keep in memory anyway).
        self.when = when

    def __enter__(self):
        def failing():
            if self.count > 0 and (self.when is None or self.when(self.store.state)):
                self.count -= 1
                raise self.error
            return self.real()
        self.patch = mock.patch.object(self.store, '_write', side_effect=failing)
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()
        return False


class Base(test_coordinator_grant.Grants):
    def on_disk(self):
        return json.loads(Path(self.service.store.path).read_text(encoding='utf-8'))

    def unrelated_save(self):
        # Any later save writes the whole state: a last-use stamp, another route's write.
        self.service.store.save()

    def failing(self, error=None, when=None):
        return SaveFails(self.service.store, error, when=when)

    def audit_actions(self, state=None):
        return [(e['action'], e['outcome']) for e in (state or self.service.state)['audit']]

    def committed(self, state=None):
        """The committed audit entries (a refusal's entry is not a change)."""
        return [(e['action'], e.get('reason')) for e in (state or self.service.state)['audit']
                if e['outcome'] == 'committed']

    @contextlib.contextmanager
    def secrets_issued(self):
        """The secrets ``http_auth.new_token`` hands out meanwhile: a failed request never returned them."""
        issued, real = [], http_auth.new_token

        def record():
            issued.append(real())
            return issued[-1]
        with mock.patch.object(http_auth, 'new_token', side_effect=record):
            yield issued

    def failed(self, answer, fault):
        self.assertEqual(0, fault.count, 'the change\'s own save did not fail')
        self.assertFalse(200 <= answer.status < 300, (answer.status, answer.data))

    def whoami(self, token):
        return self.request('GET', '/v1/sessions/current', token=token).status

    def tasks(self, token, project=None):
        return self.request('GET', '/v1/projects/%s/tasks' % (project or self.project), token=token).status

    def agent_me(self, secret):
        return self.request('GET', '/v1/agents/me', token=secret).status

    def login_status(self, name, password):
        return self.request('POST', '/v1/sessions', {'username': name, 'password': password}).status

    def coordinator_grants(self, state=None):
        return ((state or self.service.state).get('coordinator_grants') or {}).get(self.project, {})

    def reset(self, name):
        made = self.request('POST', '/v1/accounts/%s/reset' % self.ids[name], {}, token=self.admin)
        self.assertEqual(201, made.status, made.data)
        return made.data['reset_value']

    def redeem(self, name, value, password):
        return self.request('POST', '/v1/accounts/%s/reset/redeem' % self.ids[name],
                            {'reset_value': value, 'new_password': password})

    def password_of(self, name):
        return self.service.state['users'][self.ids[name]]['password']


class CoordinatorGrantTests(Base):
    def test_a_grant_whose_save_fails_is_not_in_force_nor_written_later(self):
        task, contribution = self.deliver(self.tokens['carl'])
        # Only the grant's own save fails: an unconditional fault was spent by the authentication's
        # last-use stamp, and the grant was never attempted (review of .487 F6).
        granted = lambda state: self.agent_id in (state.get('coordinator_grants') or {}).get(self.project, {})
        reached = []
        real = self.service.grant_coordinator

        def grant_coordinator(*args, **kwargs):
            reached.append(True)
            return real(*args, **kwargs)
        with mock.patch.object(self.service, 'grant_coordinator', side_effect=grant_coordinator), \
                self.failing(when=granted) as fault:
            failed = self.grant()
        self.assertEqual([True], reached)
        self.failed(failed, fault)
        self.assertEqual(500, failed.status, failed.data)
        self.assertNotIn(self.agent_id, (self.service.state.get('coordinator_grants') or {}).get(self.project, {}))
        self.unrelated_save()
        self.assertNotIn(self.agent_id, (self.on_disk().get('coordinator_grants') or {}).get(self.project, {}))
        self.assertNotIn(('agents.coordinator.grant', 'committed'), self.audit_actions())
        self.assertEqual(403, self.approve(self.agent, task, contribution).status)
        # The same request again works, and then the grant is in force.
        self.assertEqual(200, self.grant().status)
        self.assertIn(self.agent_id, self.on_disk()['coordinator_grants'][self.project])
        self.assertEqual(201, self.approve(self.agent, task, contribution).status)

    def test_a_grant_whose_save_waited_too_long_for_the_lock_is_not_in_force(self):
        task, contribution = self.deliver(self.tokens['carl'])
        granted = lambda state: self.agent_id in (state.get('coordinator_grants') or {}).get(self.project, {})
        self.assertIsNone(self.service.store.unsaved_since)
        with self.failing(TimeoutError('the state lock is held'), when=granted) as fault:
            failed = self.grant()
        self.assertEqual(0, fault.count, 'the grant\'s own save did not fail')
        self.assertNotIn(failed.status, (200, 201), failed.data)
        self.assertNotIn(self.agent_id, (self.service.state.get('coordinator_grants') or {}).get(self.project, {}))
        # Memory equals the file again, so the lock wait's unsaved mark went with the grant (review of .487 F5).
        self.assertIsNone(self.service.store.unsaved_since)
        self.unrelated_save()
        self.assertNotIn(self.agent_id, (self.on_disk().get('coordinator_grants') or {}).get(self.project, {}))
        self.assertEqual(403, self.approve(self.agent, task, contribution).status)


class WideningTests(Base):
    """Each widening whose own save fails: nothing in memory, nothing on disk later, not in force, and a retry works."""

    def test_grant_release(self):
        _, kite = self.agent_with_id('olive', 'Kite')          # a release grant needs the agent's account to be an owner
        held = lambda state: kite in (state.get('release_grants') or {}).get(self.project, {})
        before = self.committed()
        with self.failing(when=held) as fault:
            failed = self.request('PUT', '/v1/projects/%s/agents/%s/release' % (self.project, kite), {},
                                  token=self.tokens['olive'])
        self.failed(failed, fault)
        self.assertFalse(held(self.service.state))
        self.unrelated_save()
        self.assertFalse(held(self.on_disk()))
        self.assertEqual(before, self.committed())
        self.assertNotIn(('agents.release.grant', 'committed'), self.audit_actions())
        self.assertIsNone(self.listed()[kite]['release'])
        again = self.request('PUT', '/v1/projects/%s/agents/%s/release' % (self.project, kite), {},
                             token=self.tokens['olive'])
        self.assertEqual(200, again.status, again.data)
        self.assertTrue(held(self.on_disk()))
        self.assertIs(True, self.listed()[kite]['release']['effective'])

    def test_set_agent_approval_to_by_agent_after_by_person(self):
        path = '/v1/projects/%s/agent-approval' % self.project
        mode = lambda state: (state.get('agent_approval') or {}).get(self.project, {}).get('mode')
        self.assertEqual(200, self.request('PUT', path, {'agent_approval': 'by-person'}, token=self.tokens['olive']).status)
        self.assertEqual(200, self.grant().status)
        sibling, _ = self.agent_with_id('cora', 'Egret')
        task, contribution = self.deliver(sibling)
        self.assertEqual(403, self.approve(self.agent, task, contribution).status)     # strict, as before
        before = self.committed()
        with self.failing(when=lambda state: mode(state) == 'by-agent') as fault:
            failed = self.request('PUT', path, {'agent_approval': 'by-agent'}, token=self.tokens['olive'])
        self.failed(failed, fault)
        self.assertEqual('by-person', mode(self.service.state))
        self.unrelated_save()
        self.assertEqual('by-person', mode(self.on_disk()))
        self.assertEqual(before, self.committed())
        self.assertEqual(403, self.approve(self.agent, task, contribution).status)
        self.assertEqual('by-person', self.request('GET', '/v1/projects/%s' % self.project,
                                                    token=self.tokens['olive']).data.get('agent_approval', {}).get('mode', 'by-person'))
        self.assertEqual(200, self.request('PUT', path, {'agent_approval': 'by-agent'}, token=self.tokens['olive']).status)
        self.assertEqual('by-agent', mode(self.on_disk()))
        self.assertEqual(201, self.approve(self.agent, task, contribution).status)

    def test_set_member_promotion_of_a_contributor_to_coordinator(self):
        task, contribution = self.deliver(self.tokens['olive'])
        self.assertEqual(403, self.approve(self.tokens['carl'], task, contribution).status)
        role = lambda state: state['memberships'][self.project].get(self.ids['carl'])
        before = self.committed()
        with self.failing(when=lambda state: role(state) == 'coordinator') as fault:
            failed = self.member('carl', 'coordinator')
        self.failed(failed, fault)
        self.assertEqual('contributor', role(self.service.state))
        self.unrelated_save()
        self.assertEqual('contributor', role(self.on_disk()))
        self.assertEqual(before, self.committed())
        self.assertEqual(403, self.approve(self.tokens['carl'], task, contribution).status)
        self.assertEqual(200, self.member('carl', 'coordinator').status)
        self.assertEqual('coordinator', role(self.on_disk()))
        self.assertEqual(201, self.approve(self.tokens['carl'], task, contribution).status)

    def test_set_member_of_a_new_member(self):
        dana = self.create_account(self.admin, 'dana', 'dana-password-1')
        role = lambda state: state['memberships'][self.project].get(dana)
        with self.failing(when=lambda state: role(state) == 'contributor') as fault:
            failed = self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, dana), {'role': 'contributor'},
                                  token=self.tokens['olive'])
        self.failed(failed, fault)
        self.assertNotIn(dana, self.service.state['memberships'][self.project])
        self.unrelated_save()
        self.assertNotIn(dana, self.on_disk()['memberships'][self.project])
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s' % (self.project, dana), {'role': 'contributor'},
                                           token=self.tokens['olive']).status)
        self.assertEqual('contributor', role(self.on_disk()))

    def test_set_project_grant(self):
        path = '/v1/accounts/%s/project-grant' % self.ids['carl']
        held = lambda state: bool(state['users'][self.ids['carl']].get('project_grant'))
        self.assertFalse(held(self.service.state))
        before = self.committed()
        with self.failing(when=held) as fault:
            failed = self.request('PUT', path, {'limit': 3}, token=self.admin)
        self.failed(failed, fault)
        self.assertFalse(held(self.service.state))
        self.unrelated_save()
        self.assertFalse(held(self.on_disk()))
        self.assertEqual(before, self.committed())
        carl = self.service.authenticate(self.tokens['carl'])
        self.assertEqual((False, 'no-grant'), tuple(self.service.host_creation(carl)[k] for k in ('allowed', 'reason')))
        self.assertEqual(200, self.request('PUT', path, {'limit': 3}, token=self.admin).status)
        self.assertTrue(held(self.on_disk()))
        self.assertEqual((True, 3), tuple(self.service.host_creation(carl)[k] for k in ('allowed', 'limit')))

    def test_issue_credential(self):
        count = lambda state: len(state['credential_tokens'])
        start, before = count(self.service.state), self.committed()
        with self.secrets_issued() as issued, self.failing(when=lambda state: count(state) > start) as fault:
            failed = self.request('POST', '/v1/projects/%s/worker-credentials' % self.project, {'label': 'worker'},
                                  token=self.tokens['olive'])
        self.failed(failed, fault)
        self.assertTrue(issued)
        self.assertEqual(start, count(self.service.state))
        self.unrelated_save()
        self.assertEqual(start, count(self.on_disk()))
        self.assertEqual(before, self.committed())
        for secret in issued:
            self.assertEqual(401, self.tasks(secret))
        made = self.request('POST', '/v1/projects/%s/worker-credentials' % self.project, {'label': 'worker'},
                            token=self.tokens['olive'])
        self.assertEqual(201, made.status, made.data)
        self.assertEqual(start + 1, count(self.on_disk()))
        self.assertEqual(200, self.tasks(made.data['credential']['secret']))

    def test_create_agent(self):
        agents = lambda state: len(state['agents'])
        start, tokens, before = agents(self.service.state), len(self.service.state['credential_tokens']), self.committed()
        body = {'name': 'Wren', 'working_directory': '/home/x/Wren', 'projects': [self.project]}
        with self.secrets_issued() as issued, self.failing(when=lambda state: agents(state) > start) as fault:
            failed = self.request('POST', '/v1/agents', body, token=self.tokens['carl'])
        self.failed(failed, fault)
        self.assertTrue(issued)
        self.assertEqual(start, agents(self.service.state))
        self.assertEqual(tokens, len(self.service.state['credential_tokens']))
        self.unrelated_save()
        self.assertEqual(start, agents(self.on_disk()))
        self.assertEqual(tokens, len(self.on_disk()['credential_tokens']))
        self.assertEqual(before, self.committed())
        self.assertEqual([], self.request('GET', '/v1/agents', token=self.tokens['carl']).data['items'])
        for secret in issued:
            self.assertEqual(401, self.agent_me(secret))
        # The name is free again: the same request works and its secret does.
        made = self.request('POST', '/v1/agents', body, token=self.tokens['carl'])
        self.assertEqual(201, made.status, made.data)
        self.assertEqual(start + 1, agents(self.on_disk()))
        self.assertEqual(200, self.agent_me(made.data['credential']['secret']))

    def test_enable_agent_after_a_disable(self):
        path = '/v1/agents/%s/' % self.agent_id
        enabled = lambda state: state['agents'][self.agent_id]['enabled']
        self.assertEqual(200, self.request('POST', path + 'disable', {}, token=self.tokens['cora']).status)
        self.assertEqual(401, self.agent_me(self.agent))
        before = self.committed()
        with self.failing(when=enabled) as fault:
            failed = self.request('POST', path + 'enable', {}, token=self.tokens['cora'])
        self.failed(failed, fault)
        self.assertFalse(enabled(self.service.state))
        self.unrelated_save()
        self.assertFalse(enabled(self.on_disk()))
        self.assertEqual(before, self.committed())
        shown = self.request('GET', path.rstrip('/'), token=self.tokens['cora']).data
        self.assertIs(False, (shown.get('agent') or shown)['enabled'])
        self.assertNotEqual(201, self.request('POST', path + 'credentials', {}, token=self.tokens['cora']).status)
        self.assertEqual(200, self.request('POST', path + 'enable', {}, token=self.tokens['cora']).status)
        self.assertTrue(enabled(self.on_disk()))
        self.assertEqual(201, self.request('POST', path + 'credentials', {}, token=self.tokens['cora']).status)

    def other_project(self, name):
        project = self.create_project(self.admin, name)
        self.assertEqual(200, self.request('PUT', '/v1/projects/%s/members/%s' % (project, self.ids['cora']),
                                           {'role': 'coordinator'}, token=self.admin).status)
        return project

    def test_update_agent_adding_a_project(self):
        beta = self.other_project('Beta')
        path = '/v1/agents/%s' % self.agent_id
        projects = lambda state: state['agents'][self.agent_id]['projects']
        granted = lambda: [e for e in self.service.state['audit'] if e['action'] == 'agents.project.granted']
        start, before = len(granted()), self.committed()
        self.assertNotEqual(200, self.tasks(self.agent, beta))
        with self.failing(when=lambda state: beta in projects(state)) as fault:
            failed = self.request('PATCH', path, {'projects': [self.project, beta]}, token=self.tokens['cora'])
        self.failed(failed, fault)
        self.assertEqual([self.project], projects(self.service.state))
        self.unrelated_save()
        self.assertEqual([self.project], projects(self.on_disk()))
        self.assertEqual(start, len(granted()))
        self.assertEqual(before, self.committed())
        self.assertNotEqual(200, self.tasks(self.agent, beta))
        self.assertEqual(200, self.request('PATCH', path, {'projects': [self.project, beta]}, token=self.tokens['cora']).status)
        self.assertEqual([self.project, beta], projects(self.on_disk()))
        self.assertEqual(200, self.tasks(self.agent, beta))

    def test_update_agent_adding_one_project_and_removing_another(self):
        beta, gamma = self.other_project('Beta'), self.other_project('Gamma')
        path = '/v1/agents/%s' % self.agent_id
        projects = lambda state: state['agents'][self.agent_id]['projects']
        self.assertEqual(200, self.request('PATCH', path, {'projects': [self.project, beta]}, token=self.tokens['cora']).status)
        self.assertEqual(200, self.tasks(self.agent, beta))
        with self.failing(when=lambda state: gamma in projects(state)) as fault:
            failed = self.request('PATCH', path, {'projects': [self.project, gamma]}, token=self.tokens['cora'])
        self.failed(failed, fault)
        # The added project is undone, the removed one stays removed, and the removal is told as unsaved.
        self.assertEqual([self.project], projects(self.service.state))
        self.assertIsNotNone(self.service.store.unsaved_since)
        self.assertIn(('agents.update', 'committed-unsaved'), self.audit_actions())
        self.assertNotEqual(200, self.tasks(self.agent, gamma))
        self.assertNotEqual(200, self.tasks(self.agent, beta))
        self.assertEqual(200, self.tasks(self.agent))
        self.unrelated_save()
        self.assertEqual([self.project], projects(self.on_disk()))
        self.assertIn(('agents.update', 'committed-unsaved'), self.audit_actions(self.on_disk()))
        self.assertEqual(200, self.request('PATCH', path, {'projects': [self.project, gamma]}, token=self.tokens['cora']).status)
        self.assertEqual([self.project, gamma], projects(self.on_disk()))

    def test_create_user(self):
        known = lambda state: 'dana' in state['usernames']
        users, before = len(self.service.state['users']), self.committed()
        with self.failing(when=known) as fault:
            failed = self.request('POST', '/v1/accounts', {'username': 'dana'}, token=self.admin)
        self.failed(failed, fault)
        self.assertFalse(known(self.service.state))
        self.assertEqual(users, len(self.service.state['users']))
        self.unrelated_save()
        self.assertFalse(known(self.on_disk()))
        self.assertEqual(users, len(self.on_disk()['users']))
        self.assertEqual(before, self.committed())
        self.assertEqual(201, self.request('POST', '/v1/accounts', {'username': 'dana'}, token=self.admin).status)
        self.assertTrue(known(self.on_disk()))
        self.assertEqual(users + 1, len(self.on_disk()['users']))

    def test_issue_reset(self):
        count = lambda state: len(state['reset_tokens'])
        before = self.committed()
        with self.secrets_issued() as issued, self.failing(when=lambda state: count(state) > 0) as fault:
            failed = self.request('POST', '/v1/accounts/%s/reset' % self.ids['carl'], {}, token=self.admin)
        self.failed(failed, fault)
        self.assertTrue(issued)
        self.assertEqual(0, count(self.service.state))
        self.unrelated_save()
        self.assertEqual(0, count(self.on_disk()))
        self.assertEqual(before, self.committed())
        for value in issued:
            self.assertEqual(401, self.redeem('carl', value, 'carl-password-2').status)
        self.assertEqual(200, self.redeem('carl', self.reset('carl'), 'carl-password-2').status)

    def test_issue_reset_that_superseded_an_older_value(self):
        older = self.reset('carl')
        with self.secrets_issued() as issued, self.failing(when=lambda state: len(state['reset_tokens']) == 1
                                                           and not state['reset_tokens'].get(http_auth.token_hash(older))) as fault:
            failed = self.request('POST', '/v1/accounts/%s/reset' % self.ids['carl'], {}, token=self.admin)
        self.failed(failed, fault)
        # The new value is undone; the older one stays withdrawn, and says so.
        self.assertEqual({}, self.service.state['reset_tokens'])
        self.assertIsNotNone(self.service.store.unsaved_since)
        self.assertIn(('accounts.reset', 'committed-unsaved'), self.audit_actions())
        self.assertNotIn(('accounts.reset', 'committed'), self.audit_actions()[-1:])
        for value in issued + [older]:
            self.assertEqual(401, self.redeem('carl', value, 'carl-password-2').status)
        self.unrelated_save()
        self.assertEqual({}, self.on_disk()['reset_tokens'])
        self.assertEqual(200, self.redeem('carl', self.reset('carl'), 'carl-password-2').status)

    def test_redeem_reset(self):
        value = self.reset('olive')
        worker, _ = self.worker_of('olive')
        self.assertEqual(200, self.tasks(worker))
        used = lambda state: any(r['used'] for r in state['reset_tokens'].values())
        with self.failing(when=used) as fault:
            failed = self.redeem('olive', value, 'olive-password-2')
        self.failed(failed, fault)
        self.assertFalse(used(self.service.state))
        self.assertIsNotNone(self.service.store.unsaved_since)      # before any request below saves it
        self.assertIn(('accounts.reset.redeem', 'committed-unsaved'), self.audit_actions())
        # What it ended stays ended; what it would have set is not set.
        self.assertEqual(401, self.whoami(self.tokens['olive']))
        self.assertEqual(401, self.tasks(worker))
        self.assertEqual(401, self.login_status('olive', 'olive-password-2'))
        self.assertEqual(201, self.login_status('olive', 'olive-password-1'))
        self.unrelated_save()
        self.assertFalse(used(self.on_disk()))
        self.assertEqual(401, self.login_status('olive', 'olive-password-2'))
        self.assertEqual(401, self.whoami(self.tokens['olive']))
        # The same value redeems on a retry, once.
        self.assertEqual(200, self.redeem('olive', value, 'olive-password-2').status)
        self.assertEqual(201, self.login_status('olive', 'olive-password-2'))
        self.assertEqual(401, self.login_status('olive', 'olive-password-1'))
        self.assertEqual(401, self.redeem('olive', value, 'olive-password-3').status)

    def test_change_password(self):
        worker, _ = self.worker_of('olive')
        old = self.password_of('olive')
        committed = self.audit_actions().count(('accounts.password', 'committed'))
        path = '/v1/accounts/%s/password' % self.ids['olive']
        body = {'current_password': 'olive-password-1', 'new_password': 'olive-password-2'}
        with self.failing(when=lambda state: state['users'][self.ids['olive']]['password'] != old) as fault:
            failed = self.request('POST', path, body, token=self.tokens['olive'])
        self.failed(failed, fault)
        self.assertEqual(old, self.password_of('olive'))
        # The sessions and credentials it ended stay ended, and the audit says it was not saved.
        self.assertEqual(401, self.whoami(self.tokens['olive']))
        self.assertEqual(401, self.tasks(worker))
        self.assertIn(('accounts.password', 'committed-unsaved'), self.audit_actions())
        self.assertEqual(committed, self.audit_actions().count(('accounts.password', 'committed')))
        self.assertEqual(401, self.login_status('olive', 'olive-password-2'))
        token = self.login('olive', 'olive-password-1')[0]
        self.unrelated_save()
        self.assertEqual(old, self.on_disk()['users'][self.ids['olive']]['password'])
        self.assertEqual(401, self.tasks(worker))
        self.assertEqual(200, self.request('POST', path, body, token=token).status)
        self.assertNotEqual(old, self.on_disk()['users'][self.ids['olive']]['password'])
        self.assertEqual(201, self.login_status('olive', 'olive-password-2'))
        self.assertEqual(401, self.login_status('olive', 'olive-password-1'))

    def test_login(self):
        sessions = lambda state: len(state['sessions'])
        start, before = sessions(self.service.state), self.committed()
        with self.secrets_issued() as issued, self.failing(when=lambda state: sessions(state) > start) as fault:
            failed = self.request('POST', '/v1/sessions', {'username': 'carl', 'password': 'carl-password-1'})
        self.failed(failed, fault)
        self.assertTrue(issued)
        self.assertEqual(start, sessions(self.service.state))
        self.unrelated_save()
        self.assertEqual(start, sessions(self.on_disk()))
        self.assertEqual(before, self.committed())
        for secret in issued:
            self.assertEqual(401, self.whoami(secret))
        token = self.login('carl', 'carl-password-1')[0]
        self.assertEqual(start + 1, sessions(self.on_disk()))
        self.assertEqual(200, self.whoami(token))

    def test_bootstrap_superuser(self):
        store = http_auth.Store(self.tmp_path / 'fresh' / 'state.json')
        with self.assertRaises(OSError), SaveFails(store, when=lambda state: bool(state['users'])) as fault:
            http_auth.Service.bootstrap_superuser(store, 'boss', 'boss-password-1')
        self.assertEqual(0, fault.count)
        self.assertEqual({}, store.state['users'])
        self.assertEqual({}, store.state['usernames'])
        self.assertEqual([], store.state['audit'])
        store.save()
        self.assertEqual({}, json.loads(store.path.read_text(encoding='utf-8'))['users'])
        user = http_auth.Service.bootstrap_superuser(store, 'boss', 'boss-password-1')
        self.assertIn(user['id'], json.loads(store.path.read_text(encoding='utf-8'))['users'])

    def test_register_host_created_is_kept_because_the_host_already_created_it(self):
        """The one widening-shaped write that is NOT taken back: the host has already created the project, so its
        web record is a fact; kept in memory and unsaved, the creator's same request registers it (kittrial-5bb.156,
        tests/test_http_project_create.py HeldLockTests). Undoing it would leave a host project nobody owns."""
        principal = self.service.authenticate(self.admin)
        known = lambda state: 'newproj' in state['projects']
        with self.assertRaises(OSError) as raised, self.failing(when=known) as fault:
            self.service.register_host_created(principal, 'newproj', 'Newproj', {'adopted': True})
        self.assertEqual(0, fault.count)
        # Kept, and said to be kept: marked unsaved, and the route's _mutate audits it committed-unsaved
        # (review of .487 F4b).
        self.assertTrue(getattr(raised.exception, 'kept_narrowing', False))
        self.assertIsNotNone(self.service.store.unsaved_since)
        self.assertIn('newproj', self.service.state['projects'])
        self.assertEqual({self.ids_admin(): 'owner'}, self.service.state['memberships']['newproj'])
        self.unrelated_save()
        self.assertEqual({self.ids_admin(): 'owner'}, self.on_disk()['memberships']['newproj'])

    def ids_admin(self):
        return self.admin_user['id']


class NarrowingTests(Base):
    """A narrowing whose save fails stays in force in memory, is audited committed-unsaved, and is written by the next save."""

    def kept(self, name, send, when, narrowed, refused):
        with self.failing(when=when) as fault:
            failed = send()
        self.failed(failed, fault)
        self.assertTrue(narrowed(self.service.state), 'the narrowing was undone in memory')
        self.assertIsNotNone(self.service.store.unsaved_since)      # before refused() makes requests that save
        self.assertEqual(1, self.audit_actions().count((name, 'committed-unsaved')), self.audit_actions()[-4:])
        refused()
        self.unrelated_save()
        disk = self.on_disk()
        self.assertTrue(narrowed(disk), 'the narrowing was not written by the next save')
        self.assertIn((name, 'committed-unsaved'), self.audit_actions(disk))
        self.assertIsNone(self.service.store.unsaved_since)

    def test_revoke_coordinator_stays_gone_and_is_written_by_the_next_save(self):
        task, contribution = self.deliver(self.tokens['carl'])
        self.assertEqual(200, self.grant().status)
        gone = lambda state: self.agent_id not in self.coordinator_grants(state)
        with self.failing(when=gone) as fault:
            failed = self.grant(method='DELETE')
        self.failed(failed, fault)
        self.assertTrue(gone(self.service.state))
        self.assertIsNotNone(self.service.store.unsaved_since)      # before a request below saves a stamp
        self.assertEqual(1, self.audit_actions().count(('agents.coordinator.revoke', 'committed-unsaved')))
        self.assertNotIn(('agents.coordinator.revoke', 'committed'), self.audit_actions())
        # Not written yet, so the file still holds the grant.
        self.assertFalse(gone(self.on_disk()))
        self.assertEqual(403, self.approve(self.agent, task, contribution).status)
        self.unrelated_save()
        self.assertTrue(gone(self.on_disk()))
        self.assertIn(('agents.coordinator.revoke', 'committed-unsaved'), self.audit_actions(self.on_disk()))
        self.assertEqual(403, self.approve(self.agent, task, contribution).status)

    def test_other_narrowings_stay_in_force_and_are_written_by_the_next_save(self):
        project = '/v1/projects/' + self.project
        olive = self.tokens['olive']
        with self.subTest(change='remove_member'):
            self.kept('members.remove', lambda: self.request('DELETE', '%s/members/%s' % (project, self.ids['carl']), token=olive),
                      lambda state: self.ids['carl'] not in state['memberships'][self.project],
                      lambda state: self.ids['carl'] not in state['memberships'][self.project],
                      lambda: self.assertNotEqual(200, self.tasks(self.tokens['carl'])))
        with self.subTest(change='disable_agent'):
            sibling, sibling_id = self.agent_with_id('cora', 'Egret')
            off = lambda state: not state['agents'][sibling_id]['enabled']
            self.kept('agents.disable', lambda: self.request('POST', '/v1/agents/%s/disable' % sibling_id, {},
                                                             token=self.tokens['cora']),
                      off, off, lambda: self.assertEqual(401, self.agent_me(sibling)))
        with self.subTest(change='revoke_credential'):
            worker, credential = self.worker_of('olive')
            self.assertEqual(200, self.tasks(worker))
            revoked = lambda state: bool(state['credentials'][credential].get('revoked'))
            self.kept('credentials.revoke',
                      lambda: self.request('POST', '%s/worker-credentials/%s/revoke' % (project, credential), {}, token=olive),
                      revoked, revoked, lambda: self.assertEqual(401, self.tasks(worker)))
        with self.subTest(change='set_member demotion'):
            dora = self.ids['dora'] = self.create_account(self.admin, 'dora', 'dora-password-1')
            self.tokens['dora'] = self.login('dora', 'dora-password-1')[0]
            self.assertEqual(200, self.member('dora', 'coordinator').status)
            task, contribution = self.deliver(olive)
            role = lambda state: state['memberships'][self.project].get(dora)
            self.kept('members.set', lambda: self.member('dora', 'viewer'),
                      lambda state: role(state) == 'viewer', lambda state: role(state) == 'viewer',
                      lambda: self.assertEqual(403, self.approve(self.tokens['dora'], task, contribution).status))
        with self.subTest(change='set_agent_approval to by-person'):
            path = project + '/agent-approval'
            self.assertEqual(200, self.request('PUT', path, {'agent_approval': 'by-agent'}, token=olive).status)
            self.assertEqual(200, self.grant().status)
            egret, _ = self.agent_with_id('cora', 'Ibis')
            task, contribution = self.deliver(egret)
            mode = lambda state: state['agent_approval'][self.project]['mode']
            self.kept('projects.agent_approval', lambda: self.request('PUT', path, {'agent_approval': 'by-person'}, token=olive),
                      lambda state: mode(state) == 'by-person', lambda state: mode(state) == 'by-person',
                      lambda: self.assertEqual(403, self.approve(self.agent, task, contribution).status))


class InterleavingAndReloadTests(Base):

    def test_a_kept_revocation_then_a_failed_grant_of_the_same_entry_leaves_no_grant(self):
        task, contribution = self.deliver(self.tokens['carl'])
        self.assertEqual(200, self.grant().status)
        granted = self.audit_actions().count(('agents.coordinator.grant', 'committed'))
        with self.failing(when=lambda state: self.agent_id not in self.coordinator_grants(state)) as first:
            self.failed(self.grant(method='DELETE'), first)
        self.assertNotIn(self.agent_id, self.coordinator_grants())
        with self.failing(when=lambda state: self.agent_id in self.coordinator_grants(state)) as second:
            self.failed(self.grant(), second)
        self.assertNotIn(self.agent_id, self.coordinator_grants())
        self.assertEqual(403, self.approve(self.agent, task, contribution).status)
        # The kept revocation's audit entry is still there; the grant's is not.
        self.assertIn(('agents.coordinator.revoke', 'committed-unsaved'), self.audit_actions())
        self.assertEqual(granted, self.audit_actions().count(('agents.coordinator.grant', 'committed')))
        self.unrelated_save()
        self.assertNotIn(self.agent_id, self.coordinator_grants(self.on_disk()))
        self.assertIn(('agents.coordinator.revoke', 'committed-unsaved'), self.audit_actions(self.on_disk()))
        self.assertEqual(granted, self.audit_actions(self.on_disk()).count(('agents.coordinator.grant', 'committed')))
        self.assertEqual(403, self.approve(self.agent, task, contribution).status)

    def test_the_same_with_the_revocation_still_unsaved_when_the_grant_fails(self):
        """Called on the service directly, so no request's last-use stamp saves the kept revocation in between."""
        self.assertEqual(200, self.grant().status)
        granted = self.audit_actions().count(('agents.coordinator.grant', 'committed'))
        olive = self.service.authenticate(self.tokens['olive'])
        with self.failing(when=lambda state: self.agent_id not in self.coordinator_grants(state)) as first:
            self.failed(self.grant(method='DELETE'), first)
        self.assertIsNotNone(self.service.store.unsaved_since)
        with self.failing(when=lambda state: self.agent_id in self.coordinator_grants(state)) as second:
            with self.assertRaises(OSError):
                self.service.grant_coordinator(olive, self.project, self.agent_id)
        self.assertEqual(0, second.count)
        self.assertNotIn(self.agent_id, self.coordinator_grants())
        self.assertIsNotNone(self.service.store.unsaved_since)
        self.assertIn(('agents.coordinator.revoke', 'committed-unsaved'), self.audit_actions())
        self.assertEqual(granted, self.audit_actions().count(('agents.coordinator.grant', 'committed')))
        self.assertIn(self.agent_id, self.coordinator_grants(self.on_disk()))      # the file still holds the old grant
        self.unrelated_save()
        self.assertNotIn(self.agent_id, self.coordinator_grants(self.on_disk()))
        self.assertIn(('agents.coordinator.revoke', 'committed-unsaved'), self.audit_actions(self.on_disk()))

    def reloaded(self):
        return http_auth.Service(http_auth.Store(self.service.store.path), public_url='https://office.example.invalid').state

    def test_reload_after_a_failed_widening_holds_no_grant(self):
        with self.failing(when=lambda state: self.agent_id in self.coordinator_grants(state)) as fault:
            self.failed(self.grant(), fault)
        self.assertNotIn(self.agent_id, self.coordinator_grants(self.reloaded()))
        self.unrelated_save()
        self.assertNotIn(self.agent_id, self.coordinator_grants(self.reloaded()))

    def test_reload_after_a_kept_narrowing_with_no_later_save_holds_the_grant_again(self):
        """The documented limit: an unsaved narrowing is lost if the service stops before the next save.

        The owner was told the outcome could not be confirmed (the request answered an error, not success), and
        must do it again; until then it holds in memory for every request this service answers.
        """
        self.assertEqual(200, self.grant().status)
        gone = lambda state: self.agent_id not in self.coordinator_grants(state)
        with self.failing(when=gone) as fault:
            failed = self.grant(method='DELETE')
        self.failed(failed, fault)
        self.assertTrue(gone(self.service.state))
        self.assertIn(('agents.coordinator.revoke', 'committed-unsaved'), self.audit_actions())
        self.assertFalse(gone(self.reloaded()), 'a reload would have held the narrowing: the documented limit changed')
        self.assertNotIn(('agents.coordinator.revoke', 'committed-unsaved'), self.audit_actions(self.reloaded()))
        self.assertGreaterEqual(failed.status, 500, failed.data)


class UpdateAgentTests(Base):
    """The two update_agent widenings the r1 tests did not reach (review of .487 F6, mutants M17 and M18)."""

    def test_a_re_enable_whose_save_fails_is_undone(self):
        path = '/v1/agents/%s' % self.agent_id
        enabled = lambda state: state['agents'][self.agent_id].get('enabled')
        self.assertEqual(200, self.request('PATCH', path, {'enabled': False}, token=self.tokens['cora']).status)
        self.assertEqual(401, self.agent_me(self.agent))
        before = self.committed()
        with self.failing(when=enabled) as fault:
            failed = self.request('PATCH', path, {'enabled': True}, token=self.tokens['cora'])
        self.failed(failed, fault)
        self.assertFalse(enabled(self.service.state))
        self.assertIsNone(self.service.store.unsaved_since)
        self.unrelated_save()
        self.assertFalse(enabled(self.on_disk()))
        self.assertEqual(before, self.committed())
        self.assertNotEqual(201, self.request('POST', path + '/credentials', {}, token=self.tokens['cora']).status)
        self.assertEqual(200, self.request('PATCH', path, {'enabled': True}, token=self.tokens['cora']).status)
        self.assertTrue(enabled(self.on_disk()))

    def test_a_claim_limit_raised_from_none_whose_save_fails_is_undone_exactly(self):
        path = '/v1/agents/%s' % self.agent_id
        record = lambda state: state['agents'][self.agent_id]
        prior = dict(record(self.service.state))
        default = http_auth.claim_limit.default_limit(prior)
        with self.failing(when=lambda state: record(state).get('claim_limit') == default + 3) as fault:
            failed = self.request('PATCH', path, {'claim_limit': default + 3}, token=self.tokens['cora'])
        self.failed(failed, fault)
        self.assertEqual(prior, record(self.service.state))
        self.assertIsNone(self.service.store.unsaved_since)
        self.unrelated_save()
        self.assertEqual(prior.get('claim_limit'), record(self.on_disk()).get('claim_limit'))
        self.assertEqual(200, self.request('PATCH', path, {'claim_limit': default + 3}, token=self.tokens['cora']).status)
        self.assertEqual(default + 3, record(self.on_disk())['claim_limit'])

    def test_a_project_added_whose_save_waited_too_long_leaves_no_unsaved_mark(self):
        beta = WideningTests.other_project(self, 'Beta')
        path = '/v1/agents/%s' % self.agent_id
        projects = lambda state: state['agents'][self.agent_id]['projects']
        self.assertIsNone(self.service.store.unsaved_since)
        with self.failing(TimeoutError('the state lock is held'), when=lambda state: beta in projects(state)) as fault:
            failed = self.request('PATCH', path, {'projects': [self.project, beta]}, token=self.tokens['cora'])
        self.failed(failed, fault)
        self.assertEqual([self.project], projects(self.service.state))
        # Memory equals the file, so the lock wait's unsaved mark went with the change (review of .487 F5).
        self.assertIsNone(self.service.store.unsaved_since)


class AuditCapTests(Base):
    """At the audit cap, an undone widening leaves no event of itself behind (review of .487 F1)."""

    LIMIT = 60

    def fill(self):
        audit = self.service.state['audit']
        while len(audit) < self.LIMIT:
            audit.append({'time': '2026-01-01T00:00:00Z', 'request_id': None, 'user_id': None, 'actor': None,
                          'credential_id': None, 'project_id': None, 'action': 'filler', 'outcome': 'committed',
                          'reason': None})

    def test_update_agent_adding_a_project_at_the_cap(self):
        beta = WideningTests.other_project(self, 'Beta')
        path = '/v1/agents/%s' % self.agent_id
        projects = lambda state: state['agents'][self.agent_id]['projects']
        granted = lambda state: [e for e in state['audit'] if e['action'] == 'agents.project.granted'
                                 and e.get('project_id') == beta]
        with mock.patch.object(http_auth, 'AUDIT_LIMIT', self.LIMIT):
            self.fill()
            with self.failing(when=lambda state: beta in projects(state)) as fault:
                failed = self.request('PATCH', path, {'projects': [self.project, beta]}, token=self.tokens['cora'])
            self.failed(failed, fault)
            self.assertEqual([self.project], projects(self.service.state))
            self.assertEqual([], granted(self.service.state))
            self.assertLessEqual(len(self.service.state['audit']), self.LIMIT)
            self.unrelated_save()
            self.assertEqual([], granted(self.on_disk()))
            self.assertEqual(200, self.request('PATCH', path, {'projects': [self.project, beta]},
                                               token=self.tokens['cora']).status)
            self.assertEqual(1, len(granted(self.on_disk())))

    def test_a_coordinator_grant_at_the_cap(self):
        granted = lambda state: self.agent_id in (state.get('coordinator_grants') or {}).get(self.project, {})
        with mock.patch.object(http_auth, 'AUDIT_LIMIT', self.LIMIT):
            self.fill()
            with self.failing(when=granted) as fault:
                failed = self.grant()
            self.failed(failed, fault)
            self.assertFalse(granted(self.service.state))
            self.unrelated_save()
            self.assertNotIn(('agents.coordinator.grant', 'committed'), self.audit_actions(self.on_disk()))
            self.assertLessEqual(len(self.on_disk()['audit']), self.LIMIT)


class AfterTheReplaceTests(Base):
    """Nothing after the atomic replace makes a save raise (review of .487 F2)."""

    class Stderr(io.StringIO):
        def write(self, text):
            if 'saved again' in text:
                raise BrokenPipeError(32, 'Broken pipe')
            return super().write(text)

    def test_a_broken_log_stream_after_the_replace_leaves_a_saved_grant_saved(self):
        task, contribution = self.deliver(self.tokens['carl'])
        granted = lambda state: self.agent_id in (state.get('coordinator_grants') or {}).get(self.project, {})
        self.service.store.unsaved_since = self.service.store.clock()     # as an earlier lock wait leaves it
        with mock.patch.object(sys, 'stderr', self.Stderr()):
            answer = self.grant()
        self.assertEqual(200, answer.status, answer.data)
        self.assertIsNone(self.service.store.unsaved_since)
        self.assertTrue(granted(self.service.state))
        self.assertTrue(granted(self.on_disk()))
        self.assertIn(('agents.coordinator.grant', 'committed'), self.audit_actions())
        self.assertEqual(201, self.approve(self.agent, task, contribution).status)

    def test_a_lock_release_that_raises_after_the_write_is_a_saved_state(self):
        real = http_auth.file_lock

        @contextlib.contextmanager
        def releasing_badly(*args, **kwargs):
            with real(*args, **kwargs):
                yield
            raise OSError(5, 'Input/output error')
        self.service.state['cursor_key'] = 'after-the-replace'
        with mock.patch.object(http_auth, 'file_lock', side_effect=releasing_badly):
            self.service.store.save()
        self.assertEqual('after-the-replace', self.on_disk()['cursor_key'])

    def test_a_failure_before_the_replace_still_raises(self):
        self.service.state['cursor_key'] = 'before-the-replace'
        with mock.patch.object(http_auth.os, 'replace', side_effect=OSError(28, 'No space left on device')):
            with self.assertRaises(OSError):
                self.service.store.save()
        self.assertNotEqual('before-the-replace', self.on_disk()['cursor_key'])


class AgentCredentialRevokeTests(Base):
    def test_a_revoke_whose_save_fails_holds_and_is_audited_as_unsaved(self):
        """Kept like every narrowing, and its event says so (review of .487 F4a)."""
        credential_id = next(key for key, value in self.service.state['credentials'].items()
                             if value.get('agent_id') == self.agent_id and not value.get('revoked'))
        revoked = lambda state: state['credentials'].get(credential_id, {}).get('revoked') is True
        events = lambda state: [e['outcome'] for e in state['audit'] if e['action'] == 'agents.credentials.revoked'
                                and e.get('target_credential_id') == credential_id]
        with self.failing(when=revoked) as fault:
            failed = self.request('POST', '/v1/agents/%s/credentials/%s/revoke' % (self.agent_id, credential_id),
                                  {}, token=self.tokens['cora'])
        self.failed(failed, fault)
        self.assertTrue(revoked(self.service.state))
        self.assertEqual(['committed-unsaved'], events(self.service.state))
        self.assertIsNotNone(self.service.store.unsaved_since)
        self.assertEqual(401, self.agent_me(self.agent))
        self.unrelated_save()
        self.assertTrue(revoked(self.on_disk()))
        self.assertEqual(['committed-unsaved'], events(self.on_disk()))


if __name__ == '__main__':
    unittest.main()
