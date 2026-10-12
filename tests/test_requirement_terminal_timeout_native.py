"""Timed real HTTP/endpoint/bd child proof, entirely disposable native data."""
import http.client
import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path
from unittest import mock

import test_bd_label_aliases as fixture

KIT = Path(__file__).resolve().parents[1]


class TerminalTimeoutNativeTests(fixture.RealBdLabelAliasTests):
    def test_endpoint_timeout_cannot_leave_an_abandonable_live_writer(self):
        import http_service
        import requirement_owner_records as owner
        import record_json
        import test_http_review_fixes as fixes
        root = self.root
        prior = os.environ.get('ORCHESTRA_TERMINAL_PRIOR_PROBE') == '1'
        for key, value in fixture.admin.PROJECT_SETTINGS:
            self.assertEqual(self.bd('config', 'set', key, value).returncode, 0)
        if self.bd('show', 'pp-merge-slot', '--json').returncode:
            self.assertEqual(self.bd('create', 'Merge Slot', '--id', 'pp-merge-slot',
                                     '--labels', 'gt:slot', '--json').returncode, 0)
        job = json.loads(self.bd('create', 'Timed synthetic job', '--type', 'epic', '--json').stdout)['id']

        class Harness(fixes.Harness):
            def make_backend(self):
                return http_service.EndpointBackend(sys.executable, str(KIT/'endpoint.py'),
                    str(root), service=self.service, timeout=25)

        harness = Harness(methodName='runTest'); harness.setUp()
        try:
            admin = harness.admin_token()
            account = harness.create_account(admin, 'timed-owner', 'synthetic-password-1')
            token = harness.login('timed-owner', 'synthetic-password-1')[0]
            base = '/v1/projects/pp'
            self.assertEqual(harness.request('POST', '/v1/projects',
                {'project_id':'pp','name':'Synthetic timed fixture'}, token=admin).status, 201)
            self.assertEqual(harness.request('PUT', base+'/members/'+account,
                {'role':'owner'}, token=admin).status, 200)
            mode = harness.request('GET', base+'/requirements/governance', token=token).data
            changed = harness.request('PUT', base+'/requirements/governance',
                dict(mode='simple', expected_revision=mode['revision'], expected_sha256=mode['sha256']),
                token=token, key='timed-simple')
            self.assertEqual(changed.status, 200, changed.data)
            draft = harness.request('POST', base+'/requirements', dict(kind='requirement',
                parent=job, title='Timed original', description='Synthetic content'),
                token=token, key='timed-create')
            self.assertEqual(draft.status, 201, draft.data)
            item = harness.request('POST', base+'/requirements/'+draft.data['id']+'/accept',
                dict(expected_revision=draft.data['revision'], expected_sha256=draft.data['sha256']),
                token=token, key='timed-accept')
            self.assertEqual(item.status, 200, item.data); item = item.data
            real = self.bd_path.with_name('bd-real-timed'); shutil.copy(self.bd_path, real)
            self.bd_path.write_text('''#!/usr/bin/env python3
import json,os,sys,time
from pathlib import Path
p=Path(sys.argv[0]); args=sys.argv[1:]
if 'comments' in args:
 i=args.index('comments')
 marker=p.with_name('sleep-terminal-state')
 if args[i+1]=='add' and args[i+3].startswith('Kind: requirement-owner-state-v1\\n') and marker.exists():
  marker.unlink()
  p.with_name('timed-child-started.json').write_text(json.dumps({'pid':os.getpid(),'at':time.time()}))
  time.sleep(50)
os.execv(str(p.with_name('bd-real-timed')),[str(p.with_name('bd-real-timed')),*args])
''', encoding='utf-8')
            self.bd_path.with_name('sleep-terminal-state').write_text('one own synthetic delay')
            original = http.client.HTTPConnection
            class LongConnection(original):
                def __init__(self, *args, **kwargs):
                    kwargs['timeout'] = 65
                    super().__init__(*args, **kwargs)
            started = time.monotonic()
            with mock.patch('http.client.HTTPConnection', LongConnection):
                failed = harness.request('POST', base+'/requirements/'+item['id']+'/withdraw',
                    dict(expected_revision=item['revision'], expected_sha256=item['sha256'],
                         expected_state_sha256=None, reason='Own timed writer proof'),
                    token=token, key='timed-withdraw')
            self.assertEqual(failed.status, 503, failed.data)
            elapsed = time.monotonic()-started
            view = harness.request('GET', base+'/requirements/'+item['id'], token=token).data
            pending = view['pending_transition']; self.assertIsNotNone(pending)
            fields = dict(original_operation_id=pending['operation_id'],
                expected_receipt_sha256=pending['expected_receipt_sha256'], reason='Synthetic attempt abandoned')
            abandoned = harness.request('POST', base+'/requirements/'+item['id']+'/transition/abandon',
                fields, token=token, key='timed-abandon')
            if prior:
                self.assertTrue(pending['can_abandon']); self.assertEqual(abandoned.status, 200, abandoned.data)
            else:
                self.assertFalse(pending['can_abandon']); self.assertEqual(abandoned.status, 422, abandoned.data)
                self.assertIn('retry abandonment after', str(abandoned.data))
            # Let the original 50-second writer's due time pass. No manual kill:
            # only the shipped service timeout is allowed to stop descendants.
            while time.monotonic()-started < 57:
                time.sleep(min(1, 57-(time.monotonic()-started)))
            rows = record_json.loads_rows(self.bd('export', '--all').stdout)
            row = next(row for row in rows if row['id']==item['id'])
            state, reason = owner.state_ledger(row, allow_partial=True)
            view = harness.request('GET', base+'/requirements/'+item['id'], token=token).data
            print(json.dumps(dict(scope='actual HTTP endpoint subprocess and pinned native bd/Dolt',
                prior_probe=prior, endpoint_timeout=25, child_delay=50, elapsed_to_503=elapsed,
                abandon_status=abandoned.status, late_state_written=state is not None,
                late_reason_written=reason is not None, pending_after_due=view['pending_transition'] is not None)), flush=True)
            if prior:
                self.assertIsNotNone(state); self.assertIsNone(reason)
                self.assertIsNone(view['pending_transition'])
            else:
                self.assertIsNone(state); self.assertIsNone(reason)
                self.assertIsNotNone(view['pending_transition'])
                current = view['pending_transition']
                finished = harness.request('POST', base+'/requirements/'+item['id']+'/transition/finish',
                    dict(original_operation_id=current['operation_id'],
                         expected_receipt_sha256=current['expected_receipt_sha256']),
                    token=token, key='timed-finish')
                self.assertEqual(finished.status, 200, finished.data)
        finally:
            harness.tearDown()


def load_tests(loader, tests, pattern):
    # Borrow the native server fixture without rerunning its label-guard tests
    # after this test has deliberately created requirement labels in the tracker.
    return unittest.TestSuite([TerminalTimeoutNativeTests(
        'test_endpoint_timeout_cannot_leave_an_abandonable_live_writer')])
