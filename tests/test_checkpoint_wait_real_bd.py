"""Optional owned wait through host resume/client subprocesses and real pinned bd.

The fixture owns its loopback SQL server and stops it after this test. Native
reads and client transport are not mocked. No live deployment is used.
"""
import json
import subprocess
import sys
import unittest
import uuid
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT/'tests')]
import test_bd_label_aliases as fixture


@unittest.skipIf(fixture.endpoint is None, 'real checkpoint wait needs POSIX endpoint imports')
@unittest.skipIf(fixture.BD is None, 'no real bd binary (set ORCHESTRA_BD_BIN or put bd on PATH)')
class NativeCheckpointWaitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runtime = type('WaitRuntime', (fixture.RealBdLabelAliasTests,), {})
        cls.runtime.setUpClass()

    @classmethod
    def tearDownClass(cls):
        cls.runtime.tearDownClass()

    def test_resumed_host_and_client_project_same_native_checkpoint_without_acknowledging_it(self):
        runtime = self.runtime(methodName='runTest')
        root = runtime.root
        project = runtime.project
        for key, value in fixture.admin.PROJECT_SETTINGS:
            result = runtime.bd('config', 'set', key, value)
            self.assertEqual(result.returncode, 0, result.stderr)
        (project/'ONBOARDING.md').write_text('Synthetic worker project.', encoding='utf-8')
        actor = fixture.endpoint.execute(root, dict(project='pp', action='session',
            args=['register', '--name', 'Synthetic worker', '--request-id', str(uuid.uuid4())]))
        self.assertEqual(actor['returncode'], 0, actor)
        actor = json.loads(actor['stdout'])['session']['actor']
        made = runtime.bd('create', 'Synthetic waiting task', '--assignee', actor, '--json')
        self.assertEqual(made.returncode, 0, made.stderr)
        task = json.loads(made.stdout)['id']
        def read(action, args):
            answer = fixture.endpoint.execute(root, dict(project='pp', actor=actor, action=action, args=args))
            self.assertEqual(answer['returncode'], 0, answer)
            return json.loads(answer['stdout'])
        brief = read('brief', [task, '--json'])
        p = dict(schema_version=1, task=task, previous=None, activity_cursor=brief['activity_cursor'],
            source_commit='', branch='', intent='Check the saved wait', acceptance='Literal authored person/action',
            summary='Blocked on plan verification.', next_action='Read the coordinator answer before implementation.',
            open_items=[dict(id='plan', kind='blocker', source='synthetic-plan',
                            text='Waiting for person:coordinator to verify the acknowledged plan.')], resolved=[])
        saved = fixture.endpoint.execute(root, dict(project='pp', actor=actor, action='checkpoint',
            args=[task, '@attachment:checkpoint'], attachments={'checkpoint':dict(flag='--file', text=json.dumps(p))}))
        self.assertEqual(saved['returncode'], 0, saved)
        cid = json.loads(saved['stdout'])['comment_id']
        before = runtime.export_rows()
        resumed = subprocess.run([sys.executable, str(KIT/'worker.py'), '--root', str(root), '--project', 'pp',
            '--actor', actor, 'resume'], capture_output=True, text=True, timeout=150)
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.assertIn(p['open_items'][0]['text'], resumed.stdout)
        self.assertIn(p['next_action'], resumed.stdout)
        config = root/'synthetic-client.json'
        config.write_text(json.dumps(dict(transport='local', python=sys.executable,
            endpoint=str(KIT/'endpoint.py'), root=str(root))), encoding='utf-8')
        for _ in range(2):
            output = subprocess.run([sys.executable, str(KIT/'client.py'), '--config', str(config), '--project', 'pp',
                '--actor', actor, '--', 'work', '--mine', '--json'], capture_output=True, text=True, timeout=150)
            self.assertEqual(output.returncode, 0, output.stderr)
            wait = next(x for x in json.loads(output.stdout)['items'] if x['task'] == task)['checkpoint_wait']
            self.assertEqual((wait['checkpoint'], wait['author'], wait['status'], wait['active']),
                             (cid, actor, 'recorded', True))
            self.assertEqual(wait['items'][0]['text']['text'], p['open_items'][0]['text'])
        self.assertEqual(runtime.export_rows(), before)
        registry = json.loads((project/'.sessions.json').read_text())
        self.assertEqual((len(registry['records']), len(registry['resumes'])), (1, 1))


if __name__ == '__main__':
    unittest.main()
