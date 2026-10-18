"""kittrial-5bb.325 with a real pinned bd: the five reads and the HTTP agent route.

Every read here goes through the canonical endpoint or the real client/wait loop over
a disposable project and a pinned bd; no live deployment, no fixture is shared. The
class skips when the real bd binary or the POSIX endpoint imports are unavailable.
"""
import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
import test_bd_label_aliases as fixture

TEXT = 'Real-bd guidance for the acknowledgement move item.'


@unittest.skipIf(fixture.endpoint is None, 'real guidance move needs POSIX endpoint imports')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class GuidanceAckMoveRealBdTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = type('GuidanceMoveRuntime', (fixture.RealBdLabelAliasTests,), {})
        cls.runtime.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.runtime.tearDownClass()

    def setUp(self):
        self.fixture = self.runtime(methodName='runTest')
        self.root = self.fixture.root
        self.project = self.fixture.project

    # -- helpers ---------------------------------------------------------------
    def read(self, action, args, actor):
        answer = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': actor,
                                                      'action': action, 'args': args})
        self.assertEqual(answer['returncode'], 0, answer)
        return json.loads(answer['stdout'])

    def register(self):
        name = 'move-' + uuid.uuid4().hex[:8]
        answer = fixture.endpoint.execute(self.root, {'project': 'pp', 'action': 'session',
            'args': ['register', '--name', name, '--request-id', str(uuid.uuid4())]})
        self.assertEqual(answer['returncode'], 0, answer)
        return json.loads(answer['stdout'])['session']['actor']

    def task(self, actor):
        made = self.fixture.bd('create', 'A guidance move task', '--assignee', actor, '--json')
        self.assertEqual(made.returncode, 0, made.stderr)
        return json.loads(made.stdout)['id']

    def set_guidance(self, text=TEXT):
        import guidance
        return guidance.write_guidance(self.project, text, 'operator-1')

    def ack(self, actor, version):
        answer = fixture.endpoint.execute(self.root, {'project': 'pp', 'actor': actor,
            'action': 'guidance', 'args': ['ack', '--version', version]})
        self.assertEqual(answer['returncode'], 0, answer)
        return json.loads(answer['stdout'])

    def wait_read(self, actor):
        return self.read('wait', [], actor)

    def guidance_row(self, payload):
        return next((row for row in payload.get('items') or []
                     if row.get('task') == 'guidance'), None)

    def assert_item(self, block, version, where):
        item = block.get('item')
        self.assertIsNotNone(item, where)
        self.assertEqual(version, item['guidance_version'], where)
        self.assertEqual('you', item['whose_move'], where)
        self.assertIn(version, item['command'], where)
        self.assertIn('guidance get', item['command'], where)
        self.assertIn('guidance ack --version', item['command'], where)
        # The item names the version and the command, never the guidance text.
        self.assertNotIn(TEXT, json.dumps(item), where)

    # -- the five reads --------------------------------------------------------
    def test_work_brief_and_session_resume_carry_the_one_item(self):
        actor = self.register()
        task = self.task(actor)
        made = self.set_guidance()
        work = self.read('work', ['--mine', '--json'], actor)
        self.assert_item(work['guidance'], made['version'], 'work --mine')
        brief = self.read('brief', [task, '--json'], actor)
        self.assert_item(brief['guidance'], made['version'], 'brief')
        resumed = self.read('session', ['resume', '--request-id', str(uuid.uuid4())], actor)
        self.assert_item(resumed['guidance'], made['version'], 'session resume')
        # The guidance TEXT is not copied into any of the three answers.
        for name, payload in (('work', work), ('brief', brief), ('resume', resumed)):
            with self.subTest(reader=name):
                self.assertNotIn(TEXT, json.dumps(payload))

    def test_the_ack_clears_the_item_everywhere_and_a_new_set_returns_it(self):
        actor = self.register()
        task = self.task(actor)
        made = self.set_guidance()
        self.assert_item(self.read('work', ['--mine', '--json'], actor)['guidance'], made['version'], 'before')
        self.assert_item(self.read('brief', [task, '--json'], actor)['guidance'], made['version'], 'before')
        self.ack(actor, made['version'])
        for name, payload in (('work', self.read('work', ['--mine', '--json'], actor)),
                              ('brief', self.read('brief', [task, '--json'], actor)),
                              ('resume', self.read('session', ['resume', '--request-id', str(uuid.uuid4())], actor))):
            with self.subTest(reader=name):
                self.assertIsNone(payload['guidance']['item'])
                self.assertFalse(payload['guidance']['attention'])
        again = self.set_guidance('A second real-bd guidance text.')
        self.assertNotEqual(made['version'], again['version'])
        self.assert_item(self.read('work', ['--mine', '--json'], actor)['guidance'], again['version'], 'after a new set')

    def test_a_fresh_actor_and_an_actor_stale_on_an_older_version_both_get_the_current_item(self):
        actor = self.register()
        made = self.set_guidance()
        # The fresh actor has never acknowledged anything.
        self.assert_item(self.read('work', ['--mine', '--json'], actor)['guidance'], made['version'], 'fresh')
        # An actor whose last acknowledgement is an older version: hand-write it, as a
        # record from an older revision would be (write_guidance prunes such entries).
        meta = json.loads((self.project / '.guidance.json').read_text(encoding='utf-8'))
        meta['acknowledged'].setdefault(actor, {})['version'] = 'a' * 64
        meta['acknowledged'][actor]['acknowledged_at'] = '2026-01-01T00:00:00+00:00'
        (self.project / '.guidance.json').write_text(json.dumps(meta), encoding='utf-8')
        self.assert_item(self.read('work', ['--mine', '--json'], actor)['guidance'], made['version'], 'stale ack')

    # -- the wait projection and the real client loop --------------------------
    def test_wait_wakes_for_a_new_version_and_its_fingerprint_moves(self):
        actor = self.register()
        made = self.set_guidance()
        before = self.wait_read(actor)
        row = self.guidance_row(before)
        self.assertIsNotNone(row, before)
        self.assertEqual(made['version'], row['guidance_version'])
        self.assertTrue(str(before['fingerprint']).startswith('wait1:'))
        # An unchanged read is the same fingerprint: a waiter does not wake for it.
        self.assertEqual(before['fingerprint'], self.wait_read(actor)['fingerprint'])
        # The acknowledgement removes the row and moves the fingerprint.
        self.ack(actor, made['version'])
        after_ack = self.wait_read(actor)
        self.assertIsNone(self.guidance_row(after_ack))
        self.assertNotEqual(before['fingerprint'], after_ack['fingerprint'])
        # A new set returns the row with the new version and a new fingerprint.
        again = self.set_guidance('Real-bd guidance, second version.')
        after_set = self.wait_read(actor)
        self.assertEqual(again['version'], self.guidance_row(after_set)['guidance_version'])
        self.assertNotEqual(after_ack['fingerprint'], after_set['fingerprint'])
        # The REAL client loop returns at once for the new version (fingerprint differs
        # from the one the caller last acted on) ...
        config = self.root / 'guidance-wait-client.json'
        config.write_text(json.dumps(dict(transport='local', python=sys.executable,
                                          endpoint=str(KIT / 'endpoint.py'), root=str(self.root))),
                          encoding='utf-8')
        woke = subprocess.run([sys.executable, str(KIT / 'client.py'), '--config', str(config),
                               '--project', 'pp', '--actor', actor, '--', 'wait',
                               '--since', after_ack['fingerprint'], '--timeout', '20',
                               '--interval', '60'], capture_output=True, text=True, timeout=120)
        self.assertEqual(woke.returncode, 0, woke.stderr)
        view = json.loads(woke.stdout)
        self.assertEqual(again['version'], self.guidance_row(view)['guidance_version'])
        # ... and timeouts on the state it has already seen.
        steady = subprocess.run([sys.executable, str(KIT / 'client.py'), '--config', str(config),
                                 '--project', 'pp', '--actor', actor, '--', 'wait',
                                 '--since', view['fingerprint'], '--timeout', '2',
                                 '--interval', '60'], capture_output=True, text=True, timeout=120)
        self.assertEqual(steady.returncode, 1, steady.stdout + steady.stderr)
        self.assertTrue(json.loads(steady.stdout)['timed_out'])

    # -- the HTTP agent route --------------------------------------------------
    def test_the_http_agent_routes_withhold_the_item_until_its_clear_routes_exist(self):
        """kittrial-5bb.334: no HTTP guidance item while no HTTP route can clear it.

        The same guidance version still reaches an SSH caller unchanged, and the item
        returns on both HTTP agent routes the moment the guidance read and ack routes
        are registered - which is what kittrial-5bb.212 will do.
        """
        import re
        import http_service
        import test_http_review_fixes as fixes
        from unittest.mock import patch
        # .212 supplies the real routes now. Keep .334's native missing-route
        # scenario explicit, then restore the real table after this test.
        self.assertTrue(all(http_service.serves_web_guidance()))
        without_clear_routes = [entry for entry in http_service.ROUTES
                                if not ((entry[0] == 'GET' and entry[1].match(http_service.GUIDANCE_READ_PROBE))
                                        or (entry[0] == 'POST' and any(entry[1].match(probe)
                                            for probe in http_service.GUIDANCE_ACK_PROBES)))]
        self.assertEqual((False, False), http_service.serves_web_guidance(without_clear_routes))
        change = patch.object(http_service, 'ROUTES', without_clear_routes)
        change.start()
        self.addCleanup(change.stop)
        root = self.root

        class HttpHarness(fixes.Harness):
            def make_backend(self):
                return http_service.EndpointBackend(sys.executable, str(KIT / 'endpoint.py'),
                                                    str(root), service=self.service)

        harness = HttpHarness(methodName='runTest')
        harness.setUp()
        try:
            token = harness.admin_token()
            registered = harness.request('POST', '/v1/projects',
                                         {'project_id': 'pp', 'name': 'Guidance move'}, token=token)
            self.assertEqual(201, registered.status, registered.data)
            made = harness.request('POST', '/v1/agents',
                                   {'name': 'Kestrel', 'projects': ['pp']}, token=token)
            self.assertEqual(201, made.status, made.data)
            secret = made.data['credential']['secret']
            first = self.set_guidance()
            # 1. In the isolated table the guidance read and ack routes are absent, so
            # the item is withheld and it is not a wake row either.
            nxt = harness.request('GET', '/v1/agents/me/next', token=secret)
            self.assertEqual(200, nxt.status, nxt.data)
            self.assertFalse([item for item in nxt.data['next_actions']
                              if item.get('kind') == 'guidance'], nxt.data)
            # The attention count is the guidance item's count: zero, not the word
            # (the word is a legitimate key of the counts block).
            self.assertEqual(0, nxt.data['attention']['counts'].get('guidance'), nxt.data)
            wait = harness.request('GET', '/v1/agents/me/wait', token=secret)
            self.assertEqual(200, wait.status, wait.data)
            self.assertIsNone(self.guidance_row(wait.data), wait.data)
            steady = harness.request('GET', '/v1/agents/me/wait', token=secret)
            self.assertEqual(wait.data['fingerprint'], steady.data['fingerprint'])
            # 2. The SSH/endpoint reads are untouched by the switch: the same version is
            # still the one item on work --mine and the endpoint wait projection.
            ssh_actor = self.register()
            self.assert_item(self.read('work', ['--mine', '--json'], ssh_actor)['guidance'],
                             first['version'], 'SSH work --mine')
            self.assertEqual(first['version'],
                             self.guidance_row(self.wait_read(ssh_actor))['guidance_version'],
                             'SSH wait')
            # 3. Register the two routes the kit should serve (the .212 contract): the item
            # returns on both HTTP agent routes, and the wait fingerprint moves for it.
            routes = [
                ('GET', re.compile('^/v1/projects/(?P<pid>[^/]+)/guidance$'),
                 'project_guidance_read', False, True),
                ('POST', re.compile('^/v1/projects/(?P<pid>[^/]+)/guidance/ack$'),
                 'project_guidance_ack', False, True),
            ]
            for entry in routes:
                http_service.ROUTES.append(entry)
            try:
                nxt_after = harness.request('GET', '/v1/agents/me/next', token=secret)
                self.assertEqual(200, nxt_after.status, nxt_after.data)
                action = nxt_after.data['next_actions'][0]
                self.assertEqual('guidance', action['kind'], nxt_after.data)
                self.assertEqual(first['version'], action['guidance_version'])
                self.assertIn('guidance ack --version', action['command'])
                self.assertNotIn(TEXT, json.dumps(nxt_after.data))
                wait_after = harness.request('GET', '/v1/agents/me/wait', token=secret)
                row = self.guidance_row(wait_after.data)
                self.assertIsNotNone(row, wait_after.data)
                self.assertEqual(first['version'], row['guidance_version'])
                self.assertNotEqual(wait.data['fingerprint'], wait_after.data['fingerprint'])
                self.assertNotIn(TEXT, json.dumps(wait_after.data))
            finally:
                for entry in routes:
                    if entry in http_service.ROUTES:
                        http_service.ROUTES.remove(entry)
        finally:
            harness._stop_server()
            harness.doCleanups()


if __name__ == '__main__':
    unittest.main()
