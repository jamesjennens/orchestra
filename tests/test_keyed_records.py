import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import admin
import briefing
import handoff
import keyed_records as core
import project_creation
import requirement_governance as governance
import requirement_records
import web_merge_slot
from requirements import ValidationError


class HostJsonReaderTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.path = self.root / 'record.json'

    def readers(self):
        return {
            'keyed': core.read_host_json,
            'handoff': handoff.read_journal_json,
            'governance': governance._read_json,
            'briefing': briefing._read_history_snapshot,
            'merge-context': web_merge_slot._read_context,
            'project-creation': project_creation._read_json,
        }

    def test_shared_and_local_readers_delegate_to_the_host_adapter(self):
        self.path.write_text('{"ok":true}', encoding='utf-8')
        with mock.patch.object(admin, 'read_regular_text', wraps=admin.read_regular_text) as reader:
            for name, read in self.readers().items():
                with self.subTest(reader=name):
                    reader.reset_mock()
                    self.assertEqual(read(self.path), {'ok': True})
                    reader.assert_called_once_with(self.path)

    def test_shared_default_preserves_missing_and_load_json_damage_classes(self):
        self.assertIs(requirement_records.SPEC.read_receipt, core.read_host_json)
        with self.assertRaises(ValidationError):
            core.read_host_json(self.path)
        for payload in (b'\xff', b'{broken', b'{"a":1,"a":2}', b'{"n":NaN}',
                        b'{"s":"\\ud800"}', b'[' * 80 + b'0' + b']' * 80):
            with self.subTest(payload=payload[:20]):
                self.path.write_bytes(payload)
                with self.assertRaises(ValueError):
                    core.read_host_json(self.path)

    def test_local_readers_preserve_missing_and_damaged_failures(self):
        for name, read in self.readers().items():
            if name == 'keyed':
                continue
            self.path.unlink(missing_ok=True)
            with self.subTest(reader=name, shape='missing'), self.assertRaises(FileNotFoundError):
                read(self.path)
            for payload in (b'\xff', b'{broken', b'{"a":1,"a":2}', b'{"n":Infinity}',
                            b'{"s":"\\ud800"}'):
                with self.subTest(reader=name, shape=payload[:20]):
                    self.path.write_bytes(payload)
                    with self.assertRaises(ValueError):
                        read(self.path)

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFOs require POSIX')
    def test_every_host_reader_family_returns_from_a_fifo_under_timeout(self):
        cases = {
            'keyed-receipts': "import keyed_records as m; result=m.read_host_json(path)",
            'keyed-entries': "import keyed_records as m; result=m.read_host_json(path)",
            'requirement-records': "import keyed_records as m; result=m.read_host_json(path)",
            'proposal-receipts': "import keyed_records as m; result=m.read_host_json(path)",
            'handoff': "import handoff as m; result=m.read_journal_json(path)",
            'governance': "import requirement_governance as m; result=m._read_json(path)",
            'briefing': "import briefing as m; result=m._read_history_snapshot(path)",
            'merge-context': "import web_merge_slot as m; result=m._read_context(path)",
            'project-creation': "import project_creation as m; d=root/'project-creations'; d.mkdir(); os.mkfifo(d/'alpha.json'); result=m.read_record(root, 'alpha')",
            'artifacts': "import artifacts as m; p=root/'manifest.jsonl'; os.mkfifo(p); result=m.ArtifactStore(root).entries()",
            'proposal-sessions': "import proposal_records as m; result=m.session_names(root)",
            'proposal-feedback': "import proposal_records as m; result=m.feedback_origin(root/'.feedback.jsonl', 'feedback-'+'0'*32)",
            'review-reverts': "import review_workflow as m; os.mkfifo(root/m.JOURNAL_DIR/'bad.json'); result=m.host_revert_journal(root)",
            'work-requests': "import work as m; result=m.queue([], 'alice/session', [], request_dir=root)",
            'terminal': "import requirement_terminal as m; result=m.read_receipt(path)",
            'baselines': "import requirement_baselines as m; result=m._read(path, 10)",
            'owner-http': "import requirement_http as m; result=m.read_owner_receipt(path)",
        }
        for name, call in cases.items():
            with self.subTest(family=name):
                case = self.root / name
                case.mkdir()
                if name == 'review-reverts':
                    (case / '.integration-reverts').mkdir()
                if name in {'proposal-sessions', 'proposal-feedback'}:
                    target = case / ('.sessions.json' if name == 'proposal-sessions' else '.feedback.jsonl')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    import os as host_os
                    host_os.mkfifo(target)
                script = """import json,os,sys
from pathlib import Path
root=Path(sys.argv[1]); path=root/'record.json'
if not path.exists() and not path.is_dir():
    os.mkfifo(path)
try:
    %s
except Exception as error:
    print(json.dumps({'outcome':'error','error':type(error).__name__}))
else:
    print(json.dumps({'outcome':'return','value':result}, default=str))
""" % call
                answer = subprocess.run([sys.executable, '-c', script, str(case)], cwd=KIT,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(answer.returncode, 0, answer.stderr)
                outcome = json.loads(answer.stdout)
                if name == 'proposal-sessions':
                    self.assertEqual(outcome, {'outcome': 'return', 'value': {}})
                elif name == 'review-reverts':
                    self.assertEqual(outcome['outcome'], 'return')
                    self.assertEqual(outcome['value'][2], ['.integration-reverts/bad.json'])
                elif name == 'work-requests':
                    self.assertEqual(outcome['outcome'], 'return')
                    self.assertEqual([item['path'] for item in outcome['value']['journal_errors']],
                                     ['record.json'])
                else:
                    self.assertEqual(outcome['outcome'], 'error', outcome)
                    self.assertIn(outcome['error'], {'ArtifactError', 'ValidationError', 'ValueError'})


if __name__ == '__main__':
    unittest.main()
