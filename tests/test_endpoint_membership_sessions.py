"""Session durability and both unreadable-anchor routes through endpoint.execute.

Native reads are synthetic replays; session registry writes use a disposable
directory and the real session parser on every platform. The scoped import
helper never leaves a Windows lock stand-in in discovery's module cache.
"""
import json
import io
import subprocess
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(KIT), str(KIT / 'tests')]
from test_reference_wiring import _endpoint_module
import sessions
import reserved_comments
import client
import worker
import briefing


class EndpointMembershipSessionTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = _endpoint_module(self)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.path = self.root / 'projects' / 'example'
        (self.path / '.beads').mkdir(parents=True)
        (self.path / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        for target, name, value in (
                (self.endpoint, 'environment', lambda root: {}),
                (self.endpoint.native, 'argv', lambda root, path, actor, args, **kw: list(args)),
                (self.endpoint.fcntl, 'flock', lambda *args: None)):
            mocked = patch.object(target, name, value)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.actor = sessions.execute(self.path, 'example',
            ['register', '--name', 'Fixture', '--request-id', str(uuid.uuid4())], lambda: '')['session']['actor']

    def request(self, action, args):
        return self.endpoint.execute(self.root, dict(
            project='example', actor=self.actor, action=action, args=args))

    def session(self, args):
        with patch.object(self.endpoint.native, 'run', return_value=
                          subprocess.CompletedProcess([], 0, '[]\n', '')):
            answer = self.request('session', args)
        self.assertEqual(answer['returncode'], 0, answer)
        value = json.loads(answer['stdout'])
        self.assertEqual(value['provenance']['kit'], self.endpoint.report(KIT, 'kit'))
        return value

    def registry(self):
        return sessions.validate(json.loads((self.path / '.sessions.json').read_text(encoding='utf-8')))

    def seed_run(self):
        run = str(uuid.uuid4())
        sessions.execute(self.path, 'example', ['run', 'start', '--run-id', run,
            '--event-id', str(uuid.uuid4()), '--task', 'example-task'], lambda: '', actor=self.actor)
        return run

    def test_register_answers_after_the_registry_write_and_retry_reuses_it(self):
        request = str(uuid.uuid4())
        args = ['register', '--name', 'New fixture', '--request-id', request]
        first = self.session(args)
        self.assertEqual(self.registry()['records'][request], first['session'])
        second = self.session(args)
        self.assertTrue(second['reconciled'])
        self.assertEqual(second['session'], first['session'])
        self.assertEqual(len(self.registry()['records']), 2)

    def test_resume_answers_after_the_resume_write_and_retry_reuses_it(self):
        request = str(uuid.uuid4())
        args = ['resume', '--request-id', request]
        first = self.session(args)
        self.assertEqual(self.registry()['resumes'][request], first['resume'])
        self.assertEqual(self.session(args)['resume'], first['resume'])
        self.assertEqual(len(self.registry()['resumes']), 1)

    def test_show_answers_with_the_registered_actor_without_writing(self):
        before = (self.path / '.sessions.json').read_bytes()
        self.assertEqual(self.session(['show', self.actor])['session']['actor'], self.actor)
        self.assertEqual((self.path / '.sessions.json').read_bytes(), before)

    def test_host_resume_and_client_owned_work_show_the_authored_wait(self):
        from test_briefing import rows, checkpoint
        (self.path/'ONBOARDING.md').write_text('Synthetic project onboarding.', encoding='utf-8')
        (self.root/'deployment.private.json').write_text(json.dumps({'password':'synthetic-fixture'}), encoding='utf-8')
        data = rows()
        data[0].update(id='example-task', assignee=self.actor)
        p = checkpoint(rows(), task='example-task', open_items=[dict(id='verify', kind='dependency',
            text='Waiting for person:coordinator to verify the acknowledged plan.', source='plan-comment')],
            next_action='Read the plan answer before starting the approved implementation.')
        p['activity_cursor'] = briefing.activity_cursor(briefing.snapshot(data, 'example', 'example-task'))
        data[0]['comments'].append(dict(id='cp', author=self.actor, created_at='2026-10-01T00:00:00Z',
            text=briefing.PREFIX+json.dumps(p)))
        exported = '\n'.join(json.dumps(row) for row in data)+'\n'
        calls = []
        def native(args, env):
            calls.append(list(args))
            self.assertEqual(args, ['export', '--all'])
            return subprocess.CompletedProcess(args, 0, exported, '')
        def transport(cfg, project, actor, args, action='bd', *rest):
            self.assertEqual((project, actor), ('example', self.actor))
            return self.request(action, args)
        with patch.object(self.endpoint.native, 'run', side_effect=native), \
             patch.object(worker, 'request', side_effect=transport), \
             patch.object(sys, 'argv', ['worker.py', '--root', str(self.root), '--project', 'example',
                         '--actor', self.actor, 'resume']), \
             patch('sys.stdout', new_callable=io.StringIO) as stdout, \
             patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(worker.main(), 0)
            output = stdout.getvalue()
            self.assertIn('Waiting for person:coordinator to verify the acknowledged plan.', output)
            self.assertIn('Read the plan answer before starting', output)
            self.assertIn('Owned work (revision requests first)', output)
        config = self.root / 'client.json'
        config.write_text('{}', encoding='utf-8')
        with patch.object(self.endpoint.native, 'run', side_effect=native), \
             patch.object(client, 'request', side_effect=transport), \
             patch.object(sys, 'argv', ['client.py', '--config', str(config), '--project', 'example',
                         '--actor', self.actor, '--', 'work', '--mine', '--json']), \
             patch('sys.stdout', new_callable=io.StringIO) as stdout, \
             patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(client.main(), 0)
            wait = json.loads(stdout.getvalue())['items'][0]['checkpoint_wait']
            self.assertEqual((wait['checkpoint'], wait['author'], wait['active']), ('cp', self.actor, True))
        self.assertEqual(calls, [['export', '--all'], ['export', '--all']])
        self.assertEqual(len(self.registry()['records']), 1)
        self.assertEqual(len(self.registry()['resumes']), 1)
        self.assertEqual(data[0]['comments'][-1]['text'], briefing.PREFIX+json.dumps(p))

    def test_run_start_answers_after_the_event_write_and_retry_reuses_it(self):
        run, event = str(uuid.uuid4()), str(uuid.uuid4())
        args = ['run', 'start', '--run-id', run, '--event-id', event, '--task', 'example-task']
        self.session(args)
        self.assertIn(event, self.registry()['runs'][run]['events'])
        self.assertTrue(self.session(args)['reconciled'])
        self.assertEqual(len(self.registry()['runs'][run]['events']), 1)

    def test_run_heartbeat_answers_after_the_event_write(self):
        run, event = self.seed_run(), str(uuid.uuid4())
        self.session(['run', 'heartbeat', '--run-id', run, '--event-id', event])
        self.assertEqual(self.registry()['runs'][run]['events'][event]['kind'], 'heartbeat')

    def test_run_end_answers_after_the_event_write(self):
        run, event = self.seed_run(), str(uuid.uuid4())
        self.session(['run', 'end', '--run-id', run, '--event-id', event, '--status', 'succeeded'])
        self.assertEqual(self.registry()['runs'][run]['events'][event]['kind'], 'end')
        self.assertEqual(self.registry()['runs'][run]['status'], 'succeeded')

    def test_run_status_answers_with_the_events_without_writing(self):
        run = self.seed_run()
        before = (self.path / '.sessions.json').read_bytes()
        value = self.session(['run', 'status', '--run-id', run])
        self.assertEqual(value['run']['event_total'], 1)
        self.assertEqual((self.path / '.sessions.json').read_bytes(), before)

    def anchor_rows(self):
        def deep(task, labels):
            # Export's _type and close_reason, and pretty-printed show/list
            # answers with metadata before labels; no recovered field is authority.
            row = dict(_type='issue', id=task, title='Ordinary-looking title',
                       close_reason='', metadata='META', dependencies=[], labels=labels)
            return json.dumps(row, indent=2).replace('"META"', '{"deep":' + '[' * 751 + '0' + ']' * 751 + '}')
        return deep('example-anchor', []), deep('example-ordinary', ['reference'])

    def anchors(self, ids):
        anchor, ordinary = self.anchor_rows()
        calls = []
        def native(args, env):
            calls.append(args)
            if args == ['export', '--all']:
                # Native export is JSON Lines, while list/show are indented arrays.
                stdout = '\n'.join(x.replace('\n', '') for x in [anchor, ordinary]) + '\n'
            elif args == ['show', *ids, '--json', '--include-comments'] and ids:
                stdout = '[\n' + anchor + ',\n' + ordinary + '\n]'
            elif args[0] == 'list' and args[1:3] == ['--label', 'reference']:
                stdout = '[\n' + anchor + '\n]'
            elif args[0] == 'list':
                stdout = '[]'
            else:
                raise AssertionError('Unexpected native route: %r' % args)
            return subprocess.CompletedProcess(args, 0, stdout, 'native read warning\n')
        with patch.object(self.endpoint.native, 'run', side_effect=native):
            answer = self.request('anchors', ids)
        self.assertEqual(answer['returncode'], 0, answer)
        self.assertEqual(json.loads(answer['stdout'])['anchors'], ['example-anchor'])
        self.assertIn('Unreadable issue row(s):', answer['stderr'])
        self.assertIn('example-anchor', answer['stderr'])
        self.assertIn('example-ordinary', answer['stderr'])
        self.assertIn('native read warning', answer['stderr'])
        self.assertEqual(calls[1:], [['list', '--label', label, '--all', '--limit', '0', '--json']
                                   for label in sorted(reserved_comments.RECORD_ANCHOR_LABELS)])

    def test_anchors_show_classifies_and_reports_each_unreadable_row(self):
        self.anchors(['example-anchor', 'example-ordinary'])

    def test_anchors_export_classifies_and_reports_each_unreadable_row(self):
        self.anchors([])


if __name__ == '__main__':
    unittest.main()
