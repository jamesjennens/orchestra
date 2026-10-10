"""SECOND recovery cases through the real loopback HTTP/owner journal adapter."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import requirement_governance as governance
import requirement_http as requirements
import requirement_records as records
from http_authority import OperationJournal, journal_path
from requirements import content_hash
from test_http_review_fixes import EndpointCase, STUB
from test_http_requirements_owner import OwnerBackend


class SecondRecoveryHttpTests(EndpointCase):
    def make_backend(self):
        self.canonical_root = self.tmp / 'canonical'
        return OwnerBackend(sys.executable, str(STUB), str(self.canonical_root), service=self.service)

    def setUp(self):
        super().setUp(); self.addCleanup(self._stop_server)
        self.owner_token, self.project = self.setup_project()
        self.account = self.service.authenticate(self.owner_token).user_id
        self.path = self.canonical_root / 'projects' / self.project
        governance.initialize(self.path, self.project, self.account, 'creation-alpha')
        self.base = '/v1/projects/' + self.project

    def failed(self, key='failed-create'):
        fields = dict(kind='requirement', parent='job-1', title='Failed intent', description='No row yet.')
        self.backend.native.create_outcome = 'fail-after-preflight'
        answer = self.request('POST', self.base + '/requirements', fields, token=self.owner_token, key=key)
        self.backend.native.create_outcome = 'ok'
        self.assertEqual(answer.status, 503, answer.data)
        operation = self.backend._result_key(self.service.authenticate(self.owner_token), self.project,
                                             'requirements.create', key, None)
        path = self.path / requirements.JOURNAL / (content_hash({'operation_id': operation}) + '.json')
        return fields, operation, path

    def listing(self):
        answer = self.request('GET', self.base + '/requirements/recoveries', token=self.owner_token)
        self.assertEqual(answer.status, 200, answer.data)
        return answer.data

    def test_wrong_receipt_actor_under_the_correct_owner_journal_is_not_clearable(self):
        _, operation, path = self.failed()
        saved = json.loads(path.read_text()); saved['actor'] = 'usr_' + 'f' * 16
        path.write_text(json.dumps(saved))
        journal = OperationJournal(journal_path(self.path))
        self.assertEqual(journal.lookup(operation)['principal'], 'owner-account:' + self.account)
        item = next(x for x in self.listing()['items'] if x['operation_id'] == operation)
        self.assertFalse(item['can_clear'], item)
        self.assertNotIn('expected_receipt_sha256', item)

    def test_crash_before_receipt_write_can_be_cleared_without_allocating(self):
        fields = dict(kind='requirement', parent='job-1', title='Before receipt', description='No allocation.')
        with mock.patch.object(requirements.core, 'atomic', side_effect=RuntimeError('crash before receipt write')):
            answer = self.request('POST', self.base + '/requirements', fields,
                                  token=self.owner_token, key='before-receipt')
        self.assertEqual(answer.status, 503, answer.data)
        item = self.listing()['items'][0]
        path = self.path / requirements.JOURNAL / (content_hash({'operation_id': item['operation_id']}) + '.json')
        self.assertFalse(path.exists())
        self.assertIn('No creation receipt was written', item['message'])
        self.assertTrue(item['can_clear'], item)
        before = len(self.backend.native.writes())
        body = dict(original_operation_id=item['operation_id'],
                    expected_receipt_sha256=item['expected_receipt_sha256'], reason='Nothing was written.')
        wrong = self.request('POST', self.base + '/requirements/recoveries/clear',
            dict(body, expected_receipt_sha256='0' * 64), token=self.owner_token, key='wrong-prewrite-hash')
        self.assertEqual(wrong.status, 422, wrong.data)
        self.assertFalse(path.exists())
        cleared = self.request('POST', self.base + '/requirements/recoveries/clear', body,
                               token=self.owner_token, key='clear-prewrite')
        self.assertEqual(cleared.status, 200, cleared.data)
        saved = requirements.read_owner_receipt(path)
        self.assertEqual(saved['status'], 'released')
        self.assertIn('prewrite_identity', saved)
        self.assertEqual(len(self.backend.native.writes()), before)
        self.assertFalse(self.listing()['items'])
        self.assertEqual(self.request('POST', self.base + '/requirements', fields,
                                     token=self.owner_token, key='before-receipt').status, 422)
        self.assertEqual(self.request('POST', self.base + '/requirements', fields,
                                     token=self.owner_token, key='new-after-prewrite').status, 201)

    def test_nonregular_receipt_is_isolated_and_not_opened(self):
        _, operation, path = self.failed()
        path.unlink(); path.mkdir()
        item = next(x for x in self.listing()['items'] if x['operation_id'] == operation)
        self.assertFalse(item['can_clear'])
        self.assertIn('unreadable', item['message'])

    def test_fifo_and_a_race_to_fifo_do_not_block_the_receipt_reader(self):
        if not hasattr(os, 'mkfifo'):
            self.skipTest('POSIX FIFO creation is unavailable on this platform')
        _, _, path = self.failed()
        original = path.read_bytes(); path.unlink(); os.mkfifo(path)
        code = ('from pathlib import Path; import requirement_http as r; '
                'r.read_owner_receipt(Path(__import__("sys").argv[1]))')
        answer = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(answer.returncode, 0)
        self.assertIn('unreadable', answer.stderr)
        path.unlink(); path.write_bytes(original)
        code = '''import os,sys
from pathlib import Path
import requirement_http as r
p=Path(sys.argv[1]); original=os.open
def replaced(path, flags):
    p.unlink(); os.mkfifo(p)
    return original(path, flags)
r.os.open=replaced
r.read_owner_receipt(p)
'''
        answer = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True, text=True, timeout=5)
        self.assertNotEqual(answer.returncode, 0)
        self.assertIn('unreadable', answer.stderr)

    def test_new_recovery_has_title_time_but_legacy_receipts_do_not_invent_titles(self):
        _, operation, path = self.failed()
        item = next(x for x in self.listing()['items'] if x['operation_id'] == operation)
        self.assertEqual(item['title'], 'Failed intent')
        self.assertRegex(item['at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')
        saved = json.loads(path.read_text()); saved.pop('creation_metadata'); path.write_text(json.dumps(saved))
        legacy = next(x for x in self.listing()['items'] if x['operation_id'] == operation)
        self.assertNotIn('title', legacy)
        self.assertRegex(legacy['at'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$')

    def test_mode_switch_keeps_validated_pending_native_author_visible(self):
        first = self.request('POST', self.base + '/requirements', dict(kind='requirement', parent='job-1',
            title='Owner draft', description='Starting wording.'), token=self.owner_token, key='before-mode').data
        current = governance.current(self.path, self.project)
        governance.set_mode(self.path, self.project, 'governed', self.account, 'mode-for-contributor',
                            current['revision'], current['sha256'])
        author = '<contributor>\u202eusr_aaaaaaaaaaaaaaaa'
        self.backend.native.actor = author
        payload = dict(schema_version=1, operation_id='governed-pending', operation='revise', task=first['id'],
            kind='requirement', key='req-' + content_hash({'operation_id': self.backend._result_key(
                self.service.authenticate(self.owner_token), self.project, 'requirements.create', 'before-mode', None)})[:12],
            title='Contributor pending', description='Contributor wording.', revision=2, acceptance_state='draft')
        records.apply_native(payload, author, self.backend.native, self.path)
        current = governance.current(self.path, self.project)
        switched = self.request('PUT', self.base + '/requirements/governance', dict(mode='simple',
            expected_revision=current['revision'], expected_sha256=current['sha256']),
            token=self.owner_token, key='switch-back-simple')
        self.assertEqual(switched.status, 200, switched.data)
        route = self.base + '/requirements/' + first['id']
        detail = self.request('GET', route, token=self.owner_token)
        self.assertEqual(detail.status, 200, detail.data)
        self.assertEqual(detail.data['pending_author'], dict(text=author, omitted_chars=0))
        node = shutil.which('node')
        if node:
            root = Path(__file__).resolve().parents[1]
            web = self.tmp / 'author-web'; web.mkdir()
            (web/'package.json').write_text('{"type":"module"}')
            shutil.copytree(root/'web/js', web/'js')
            result = subprocess.run([node,
                str(root/'tests/web_owner_pending_author.mjs'), (web/'js/views/owner_requirements.js').as_uri(),
                json.dumps(detail.data)], capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
        accepted = self.request('POST', route + '/accept', dict(expected_revision=2,
            expected_sha256=detail.data['current']['sha256']), token=self.owner_token, key='accept-contributor')
        self.assertEqual(accepted.status, 200, accepted.data)

    def test_same_key_create_never_exposes_damaged_receipt_paths(self):
        fields, _, path = self.failed()
        for number, payload in enumerate((b'{broken', b'\xff', b'[]', b'{"sha256":"bad"}',
                                         b'{"a":1,"a":2}', b'[' * 80 + b'0' + b']' * 80)):
            with self.subTest(shape=number):
                path.write_bytes(payload)
                before = len(self.backend.native.writes())
                with mock.patch.object(requirements, 'read_owner_receipt', wraps=requirements.read_owner_receipt) as reader:
                    answer = self.request('POST', self.base + '/requirements', fields,
                                          token=self.owner_token, key='failed-create')
                self.assertTrue(any(Path(call.args[0]) == path for call in reader.call_args_list),
                                (answer.data, reader.call_args_list))
                self.assertEqual(answer.status, 422, answer.data)
                self.assertIn(requirements.RECOVERY_READ_ERROR, str(answer.data))
                self.assertNotIn(str(self.path), json.dumps(answer.data))
                self.assertNotIn(path.name, json.dumps(answer.data))
                self.assertEqual(len(self.backend.native.writes()), before)

    def test_governed_recovery_routes_retain_the_enable_editing_advice(self):
        _, operation, path = self.failed()
        body = dict(original_operation_id=operation, expected_receipt_sha256=content_hash(json.loads(path.read_text())),
                    reason='No content was written.')
        current = governance.current(self.path, self.project)
        governance.set_mode(self.path, self.project, 'governed', self.account, 'switch-governed',
                            current['revision'], current['sha256'])
        for method, suffix, payload in (('GET', '/recoveries', None), ('POST', '/recoveries/clear', body)):
            with self.subTest(route=suffix):
                answer = self.request(method, self.base + '/requirements' + suffix, payload,
                                      token=self.owner_token, key='governed-clear' if payload else None)
                self.assertEqual(answer.status, 422, answer.data)
                self.assertIn('enable simple editing', json.dumps(answer.data))

    def test_recovery_directory_repair_advice_is_preserved(self):
        self.failed()
        with mock.patch.object(requirements, 'recovery_projection',
                               side_effect=ValueError('Owner recovery receipts need host operator repair')):
            answer = self.request('GET', self.base + '/requirements/recoveries', token=self.owner_token)
        self.assertEqual(answer.status, 422, answer.data)
        self.assertIn('receipts need host operator repair', json.dumps(answer.data))

    def test_transient_native_read_failure_says_reload_and_reload_recovers(self):
        _, operation, _ = self.failed()
        with mock.patch.object(requirements.records, 'read_rows',
                               side_effect=OSError('private-path-sentinel /server/private')):
            item = next(x for x in self.listing()['items'] if x['operation_id'] == operation)
        self.assertFalse(item['can_clear'])
        self.assertIn('read now', item['message'])
        self.assertIn('Reload', item['message'])
        self.assertNotIn('private-path-sentinel', json.dumps(item))
        self.assertTrue(next(x for x in self.listing()['items'] if x['operation_id'] == operation)['can_clear'])

    def test_accepted_evidence_still_names_the_accepted_wording_after_a_new_draft(self):
        first = self.request('POST', self.base + '/requirements', dict(kind='requirement', parent='job-1',
            title='First wording', description='Exact accepted content.'), token=self.owner_token, key='new-content').data
        route = self.base + '/requirements/' + first['id']
        accepted = self.request('POST', route + '/accept', dict(expected_revision=first['revision'],
            expected_sha256=first['sha256']), token=self.owner_token, key='accept-content')
        self.assertEqual(accepted.status, 200, accepted.data)
        edited = self.request('PATCH', route, dict(expected_revision=accepted.data['revision'],
            expected_sha256=accepted.data['sha256'], description='Later draft wording.'),
            token=self.owner_token, key='later-draft')
        self.assertEqual(edited.status, 200, edited.data)
        detail = self.request('GET', route, token=self.owner_token)
        self.assertEqual(detail.status, 200, detail.data)
        self.assertEqual(detail.data['accepted']['sha256'], accepted.data['sha256'])
        self.assertIsNotNone(detail.data['acceptance'])
        self.assertEqual(detail.data['acceptance']['revision'], accepted.data['revision'])
        self.assertEqual(detail.data['acceptance']['record_sha256'], accepted.data['sha256'])
        self.assertNotEqual(detail.data['acceptance']['record_sha256'], edited.data['sha256'])
