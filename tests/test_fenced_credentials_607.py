"""Fenced credentials (kittrial-5bb.607): a credential an older kit cannot use.

A credential whose scope is fenced (``rooms`` today) is kept on disk only in ``fenced_credentials`` and
``fenced_credential_tokens``, never in ``credentials`` / ``credential_tokens``. An older kit's one
credential lookup (http_auth.py:2142-2143 at b1f2356c) therefore finds nothing, so its token answers 401
there and reads nothing. The new kit merges the two collections in memory, so every reader of the
credentials sees one set. The rules, each a test below:

- a fenced scope is never combined with another, belongs to an agent credential only, and holds no
  baseline read: a rooms-only credential holds ``rooms.post`` and nothing else, and every route but the
  rooms post routes refuses it (VERIFIED condition 4);
- revocation, expiry, a disabled agent and the issuer's membership are checked live, as for any credential;
- a restore with ``--credentials revoke`` revokes fenced credentials too, and the restore's shape check
  covers both collections (condition 1);
- the new kit notices a state file it did not write (an older kit's restore or save) and suspends every
  fenced credential until an operator confirms or revokes them (condition 2, as amended 11:42Z);
- the agent credential transaction never loses or resurrects a fenced credential (condition 3).

The rollback rehearsal against b1f2356c itself is tests/test_fenced_credentials_607_rollback.py.
"""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))

import admin
import http_auth
import http_authority
import http_service
from http_auth import Service, Store, token_hash
import test_lane_nudge_carrier as carrier
from test_http_state_restore_truth import ADMIN_NAME, ADMIN_PASSWORD

ROOMS = ['rooms']


def on_disk(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


class FencedCase(carrier.CarrierCase):
    """A rooms-only agent of carl, ``self.rooms`` its secret and ``self.rooms_agent`` its id."""

    def setUp(self):
        super().setUp()
        self.rooms, self.rooms_agent = self.carrier('carl', 'Scribe', scopes=ROOMS)
        self.rooms_id = self.credential_of(self.rooms_agent)['id']

    def credential_of(self, agent_id):
        found = [c for c in self.service.state['credentials'].values() if c.get('agent_id') == agent_id]
        self.assertEqual(1, len(found))
        return found[0]

    def reload(self):
        """The same state file read by a new kit, as after a restart."""
        self.service.store.save()
        store = Store(self.service.store.path)
        return store, Service(store, public_url='https://office.example.invalid')

    def tasks(self, token):
        return self.request('GET', '/v1/projects/%s/tasks' % self.project, token=token)


class TheFenceTests(FencedCase):
    def test_a_rooms_only_credential_is_invisible_to_the_older_lookup_and_reads_nothing(self):
        stored = on_disk(self.service.store.path)
        digest = token_hash(self.rooms)
        # The exact lookup of b1f2356c (http_auth.py:2142-2143) finds nothing.
        self.assertNotIn(digest, stored['credential_tokens'])
        self.assertIsNone(stored['credentials'].get(stored['credential_tokens'].get(digest)))
        self.assertNotIn(self.rooms_id, stored['credentials'])
        # It is kept, in the fenced collections only.
        self.assertEqual(self.rooms_id, stored['fenced_credential_tokens'][digest])
        self.assertEqual(ROOMS, stored['fenced_credentials'][self.rooms_id]['scopes'])
        # On the new kit it authenticates, and reads no task.
        self.assertEqual(403, self.tasks(self.rooms).status)
        credential = self.service.state['credentials'][self.rooms_id]
        issuer = self.service.state['users'][credential['user_id']]
        self.assertEqual(frozenset({http_authority.CAP_ROOMS_POST}),
                         http_authority.credential_capabilities(self.service.state, credential, issuer, self.project))

    def test_an_ordinary_credential_stays_where_it_was(self):
        stored = on_disk(self.service.store.path)
        plain = self.credential_of(self.agent_id)
        self.assertIn(plain['id'], stored['credentials'])
        self.assertNotIn(plain['id'], stored['fenced_credentials'])
        self.assertEqual(200, self.tasks(self.agent).status)

    def test_the_new_kit_reads_the_fence_back_after_a_restart(self):
        store, service = self.reload()
        self.assertIn(self.rooms_id, store.state['credentials'])
        self.assertEqual(self.rooms_id, store.state['credential_tokens'][token_hash(self.rooms)])
        self.assertNotIn('fenced_credentials', store.state)          # one set in memory
        principal = service.authenticate(self.rooms)
        self.assertEqual(self.rooms_id, principal.credential_id)

    def test_the_same_id_on_both_sides_is_refused_at_load(self):
        stored = on_disk(self.service.store.path)
        stored['credentials'][self.rooms_id] = stored['fenced_credentials'][self.rooms_id]
        Path(self.service.store.path).write_text(json.dumps(stored), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'both fenced and unfenced'):
            Store(self.service.store.path)


class ScopeRuleTests(FencedCase):
    def test_a_fenced_scope_is_never_combined(self):
        for scopes in (['rooms', 'read'], ['tasks', 'rooms'], ['rooms', 'nudges']):
            with self.subTest(scopes=scopes):
                made = self.request('POST', '/v1/agents', {'name': 'Mix', 'working_directory': '/home/x/m',
                                                           'projects': [self.project], 'scopes': scopes},
                                    token=self.tokens['carl'])
                self.assertEqual(422, made.status, made.data)
        renewed = self.request('POST', '/v1/agents/%s/credentials' % self.rooms_agent, {'scopes': ['rooms', 'read']},
                               token=self.tokens['carl'])
        self.assertEqual(422, renewed.status, renewed.data)

    def test_a_worker_credential_never_carries_one(self):
        worker = self.request('POST', '/v1/projects/%s/worker-credentials' % self.project,
                              {'label': 'w', 'scopes': ROOMS}, token=self.tokens['olive'])
        self.assertEqual(422, worker.status, worker.data)
        self.assertIn('agent credential', worker.data['error']['message'])

    def test_a_mixed_record_anyway_holds_only_the_fenced_capabilities(self):
        credential = self.service.state['credentials'][self.rooms_id]
        credential['scopes'] = ['rooms', 'read', 'tasks']
        issuer = self.service.state['users'][credential['user_id']]
        self.assertEqual(frozenset({http_authority.CAP_ROOMS_POST}),
                         http_authority.credential_capabilities(self.service.state, credential, issuer, self.project))

    def test_no_baseline_read_and_no_grant_reaches_it(self):
        credential = self.service.state['credentials'][self.rooms_id]
        issuer = self.service.state['users'][credential['user_id']]
        caps = http_authority.credential_capabilities(self.service.state, credential, issuer, self.project)
        self.assertNotIn(http_authority.CAP_READ, caps)
        self.assertEqual({'rooms'}, set(http_authority.FENCED_SCOPES))
        self.assertEqual(frozenset({http_authority.CAP_ROOMS_POST}), http_authority.SCOPE_CAPABILITIES['rooms'])

    def test_every_route_but_the_rooms_posts_refuses_it(self):
        swept, open_routes = 0, []
        for verb, pattern, name, anonymous, _csrf in http_service.ROUTES:
            if anonymous or name in http_service.FENCED_ROUTES:
                continue
            path = carrier.fill(pattern)
            with self.subTest(route=name):
                self.assertIsNotNone(path, 'no sample path matches %s' % pattern.pattern)
                answer = self.request(verb, path, {} if verb != 'GET' else None, token=self.rooms)
                swept += 1
                if answer.status != 403:
                    open_routes.append((name, answer.status))
        self.assertGreater(swept, 100)
        self.assertEqual([], open_routes)
        self.assertEqual({'rooms_post', 'rooms_item_post'}, http_service.FENCED_ROUTES)


class LiveCheckTests(FencedCase):
    def test_revoke_reaches_it_and_the_list_shows_it(self):
        listed = self.request('GET', '/v1/agents/%s' % self.rooms_agent, token=self.tokens['carl'])
        self.assertEqual(200, listed.status, listed.data)
        self.assertIn(self.rooms_id, [c['id'] for c in listed.data['credentials']])
        revoked = self.request('POST', '/v1/agents/%s/credentials/%s/revoke' % (self.rooms_agent, self.rooms_id), {},
                               token=self.tokens['carl'])
        self.assertIn(revoked.status, (200, 201, 204), revoked.data)   # the route answers 204
        self.assertEqual(401, self.tasks(self.rooms).status)
        self.assertTrue(on_disk(self.service.store.path)['fenced_credentials'][self.rooms_id]['revoked'])

    def test_a_disabled_agent_and_an_expired_credential_are_refused(self):
        self.assertEqual(200, self.request('POST', '/v1/agents/%s/disable' % self.rooms_agent, {},
                                           token=self.tokens['carl']).status)
        self.assertEqual(401, self.tasks(self.rooms).status)
        self.assertEqual(200, self.request('POST', '/v1/agents/%s/enable' % self.rooms_agent, {},
                                           token=self.tokens['carl']).status)
        self.service.state['credentials'][self.rooms_id]['expires_at'] = 0
        self.assertEqual(401, self.tasks(self.rooms).status)

    def test_the_issuer_leaving_the_project_ends_its_authority(self):
        credential = self.service.state['credentials'][self.rooms_id]
        issuer = self.service.state['users'][credential['user_id']]
        self.service.state['memberships'][self.project].pop(credential['user_id'])
        with self.assertRaises(Exception):
            http_authority.credential_capabilities(self.service.state, credential, issuer, self.project)


class DecideTests(FencedCase):
    """The live authority decision itself, beside the dispatcher's allowlist."""

    def decision(self, capability, project=None):
        principal = self.service.authenticate(self.rooms)
        return http_authority.decide(self.service.state,
                                     http_authority.authority_request(principal, project, capability))

    def test_its_own_agent_record_is_refused(self):
        with self.assertRaises(http_authority.AuthorityDenied) as refused:
            self.decision(http_authority.CAP_AGENT_SELF)
        self.assertEqual(403, refused.exception.status)

    def test_a_suspension_is_read_live(self):
        principal = self.service.authenticate(self.rooms)
        self.service.state['credentials'][self.rooms_id]['suspended'] = {'at': 'x', 'reason': 'test'}
        with self.assertRaises(http_authority.AuthorityDenied) as refused:
            http_authority.decide(self.service.state, http_authority.authority_request(
                principal, self.project, http_authority.CAP_ROOMS_POST))
        self.assertEqual(401, refused.exception.status)


class TransactionTests(FencedCase):
    """Condition 3: a failed save never resurrects or loses a fenced credential."""

    def test_a_failed_issue_takes_the_new_fenced_credential_back(self):
        agent = self.service.state['agents'][self.rooms_agent]
        before = copy.deepcopy(self.service.state['credentials'])
        with self.service.store.lock:
            with self.assertRaises(RuntimeError):
                with self.service._agent_credential_transaction(agent):
                    owner = http_auth.Principal(agent['owner'], 'carl', False, 'session', agent['owner'])
                    self.service._issue_agent_credential_locked(owner, agent)
                    raise RuntimeError('the save failed')
        self.assertEqual(before, self.service.state['credentials'])
        mine = {cid for cid, c in before.items() if c.get('agent_id') == self.rooms_agent}
        self.assertEqual({token_hash(self.rooms)},
                         {d for d, cid in self.service.state['credential_tokens'].items() if cid in mine})
        stored = on_disk(self.reload()[0].path)
        self.assertEqual([self.rooms_id], [cid for cid, c in stored['fenced_credentials'].items()
                                           if c.get('agent_id') == self.rooms_agent])

    def test_a_failed_revoke_keeps_its_effect_and_the_credential(self):
        agent = self.service.state['agents'][self.rooms_agent]
        with self.service.store.lock:
            with self.assertRaises(RuntimeError):
                with self.service._agent_credential_transaction(agent, rollback=False):
                    self.service.state['credentials'][self.rooms_id]['revoked'] = True
                    raise RuntimeError('the save failed')
        self.assertTrue(self.service.state['credentials'][self.rooms_id]['revoked'])
        self.assertEqual(self.rooms_id, self.service.state['credential_tokens'][token_hash(self.rooms)])


class DetectionTests(FencedCase):
    """Condition 2: a state file this kit did not write suspends every fenced credential."""

    def written_by_another_kit(self, change):
        self.service.store.save()
        path = Path(self.service.store.path)
        stored = on_disk(path)
        change(stored)
        path.write_text(json.dumps(stored, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        store = Store(path)
        return store, Service(store, public_url='https://office.example.invalid')

    def assert_suspended(self, store, service, ordinary_revoked=False):
        credential = store.state['credentials'][self.rooms_id]
        self.assertTrue(credential.get('suspended'))
        with self.assertRaises(http_auth.HttpError) as refused:
            service.authenticate(self.rooms)
        self.assertEqual(401, refused.exception.status)
        self.assertIn('suspended', str(refused.exception.body('r')))
        if ordinary_revoked:
            # The older kit's revoke reached the ordinary credential itself; detection adds nothing to it.
            with self.assertRaises(http_auth.HttpError) as revoked:
                service.authenticate(self.agent)
            self.assertIn('revoked', str(revoked.exception.body('r')).lower())
            return
        # The ordinary credential is not touched.
        self.assertEqual(self.agent_id, service.authenticate(self.agent).agent_id)

    def test_an_ordinary_save_by_an_older_kit_suspends_them(self):
        store, service = self.written_by_another_kit(lambda s: s['users'][self.ids['carl']].update(last_login='x'))
        self.assert_suspended(store, service)
        # It is written, so a second start still finds it suspended.
        again = Store(store.path)
        self.assertTrue(again.state['credentials'][self.rooms_id].get('suspended'))

    def test_an_older_restore_of_an_identical_backup_with_revoke_suspends_them(self):
        def restored(stored):
            for record in stored['credentials'].values():
                record['revoked'] = True          # what b1f2356c's --credentials revoke does
            for session in stored['sessions'].values():
                session['revoked'] = True
        self.assert_suspended(*self.written_by_another_kit(restored), ordinary_revoked=True)

    def test_a_stop_between_the_writer_record_and_the_replace_does_not_suspend(self):
        path = Path(self.service.store.path)
        self.service.store.save()
        real = http_auth.os.replace

        def replace(source, target):
            if str(target) == str(path):            # the writer record is replaced; the state file is not
                raise OSError('stopped')
            return real(source, target)
        with mock.patch('http_auth.os.replace', side_effect=replace):
            self.service.state['users'][self.ids['carl']]['last_login'] = 'y'
            with self.assertRaises(OSError):
                self.service.store._write()
        self.assertEqual(2, len(http_auth.read_writer_record(path)))
        store = Store(path)
        self.assertFalse(store.state['credentials'][self.rooms_id].get('suspended'))

    def test_a_missing_writer_record_with_fenced_credentials_suspends_them(self):
        self.service.store.save()
        Path(str(self.service.store.path) + http_auth.WRITER_SUFFIX).unlink()
        store = Store(self.service.store.path)
        self.assertTrue(store.state['credentials'][self.rooms_id].get('suspended'))

    def test_no_fenced_credential_means_no_writer_record_and_nothing_to_suspend(self):
        live = Path(self.tmp_path) / 'plain.json'
        Service.bootstrap_superuser(Store(live), ADMIN_NAME, ADMIN_PASSWORD)
        self.assertFalse(Path(str(live) + http_auth.WRITER_SUFFIX).exists())
        stored = on_disk(live)
        stored['users']['x'] = {}                 # written by someone else: there is nothing to suspend
        live.write_text(json.dumps(stored), encoding='utf-8')
        self.assertEqual({}, {cid: c for cid, c in Store(live).state['credentials'].items() if c.get('suspended')})

    def test_the_operator_confirms_or_revokes_them(self):
        store, _service = self.written_by_another_kit(lambda s: s['users'][self.ids['carl']].update(last_login='x'))
        del store
        text = admin.fenced_credentials(self.service.store.path, 'confirm')
        self.assertIn('1 fenced credential', text)
        store = Store(self.service.store.path)
        self.assertFalse(store.state['credentials'][self.rooms_id].get('suspended'))
        Service(store, public_url='https://office.example.invalid').authenticate(self.rooms)
        self.written_by_another_kit(lambda s: s['users'][self.ids['carl']].update(last_login='z'))
        admin.fenced_credentials(self.service.store.path, 'revoke')
        store = Store(self.service.store.path)
        self.assertTrue(store.state['credentials'][self.rooms_id]['revoked'])
        self.assertFalse(store.state['credentials'][self.rooms_id].get('suspended'))


class RestoreTests(unittest.TestCase):
    """Condition 1: the new kit's own restore revokes fenced credentials and checks both collections."""

    def setUp(self):
        import tempfile
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'backups').mkdir()
        self.live = self.root / 'http-state.json'
        Service.bootstrap_superuser(Store(self.live), ADMIN_NAME, ADMIN_PASSWORD)
        admin.backup_http_state(self.root)
        self.backed = self.root / 'backups' / admin.HTTP_STATE_JSON
        document = json.loads(self.backed.read_text(encoding='utf-8'))
        document['fenced_credentials'] = {'cred_f': {'id': 'cred_f', 'user_id': 'u', 'agent_id': 'agent_a',
                                                     'project_id': None, 'scopes': ['rooms'], 'token_hash': 'h',
                                                     'revoked': False}}
        document['fenced_credential_tokens'] = {'h': 'cred_f'}
        self.backed.write_text(json.dumps(document), encoding='utf-8')

    def test_revoke_revokes_the_fenced_credentials_too(self):
        text = admin.restore_http_state(self.root, credentials='revoke')
        restored = json.loads(self.live.read_text(encoding='utf-8'))
        self.assertTrue(restored['fenced_credentials']['cred_f']['revoked'])
        self.assertIn('fenced', text)

    def test_keep_keeps_them_and_the_restore_is_not_taken_for_another_kit(self):
        admin.restore_http_state(self.root, credentials='keep')
        store = Store(self.live)
        self.assertFalse(store.state['credentials']['cred_f']['revoked'])
        self.assertFalse(store.state['credentials']['cred_f'].get('suspended'))

    def test_the_shape_check_covers_both_collections(self):
        before = self.live.read_bytes()
        original = self.backed.read_text(encoding='utf-8')
        for key, value in (('fenced_credentials', {'cred_f': 'not a record'}),
                           ('fenced_credential_tokens', ['h'])):
            document = json.loads(original)          # one damaged collection at a time
            document[key] = value
            self.backed.write_text(json.dumps(document), encoding='utf-8')
            with self.subTest(key=key):
                with self.assertRaisesRegex(ValueError, key):
                    admin.restore_http_state(self.root, credentials='keep')
                self.assertEqual(before, self.live.read_bytes())


if __name__ == '__main__':
    unittest.main()
