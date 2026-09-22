"""Contract tests for the versioned CLI JSON/help/error interface (kittrial-5bb.7).

These tests pin the client/endpoint boundary that automation depends on:
success JSON on stdout stays parseable while warnings travel on stderr, failures
are nonzero and named, documented limits are enforced, common mistaken flags get
a hint, and existing response shapes keep working.
"""
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import briefing
import client
import native
import onboarding
import work
from test_briefing import PROJECT, TASK, checkpoint, rows

ITEM_FIELDS = {
    'task', 'title', 'owner', 'status', 'review_state', 'contribution_id', 'commit',
    'pending_review_items', 'pending_handoff_requests', 'pending_handoff_total',
    'pending_handoff_next_offset', 'lifecycle', 'lifecycle_scope',
    'lifecycle_matches_contribution', 'error',
}
BRIEF_FIELDS = {
    'task', 'title', 'owner', 'status', 'activity_cursor', 'checkpoint', 'intent',
    'acceptance', 'current_position', 'next_action', 'unresolved', 'review',
    'lifecycle', 'dependencies', 'lifecycle_scope',
    'lifecycle_matches_contribution', 'warnings', 'evidence',
}


def completed(stdout='', stderr='', returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class WorkHelpContractTests(unittest.TestCase):
    """`work --help/--json` are dependable, and help is data (exit 0), not SystemExit."""

    def test_help_is_structured_and_uses_contract_version(self):
        payload = work.queue(rows(), 'alice/session', ['--help'])
        self.assertEqual(payload['schema_version'], 1)
        self.assertEqual(payload['command'], 'work')
        self.assertEqual(payload['contract'], work.CONTRACT_VERSION)
        self.assertTrue(any(option['flag'].startswith('--limit')
                            for option in payload['options']))
        self.assertEqual(json.loads(json.dumps(payload))['limits']['limit'],
                         '%d..%d' % (work.WORK_LIMIT_MIN, work.WORK_LIMIT_MAX))

    def test_help_through_execute_does_not_run_native_commands(self):
        def run(argv):
            raise AssertionError('help must not invoke the native command runner')
        payload = work.execute(Path('.'), 'alice/session', 'work', ['--help'], {}, run)
        self.assertEqual(payload['command'], 'work')
        self.assertTrue(payload['usage'].startswith('work '))

    def test_review_and_handoff_help_are_available(self):
        for action in ('review', 'handoff'):
            with self.subTest(action=action):
                payload = work.execute(Path('.'), 'alice/session', action, ['-h'], {},
                                       lambda argv: None)
                self.assertEqual(payload['command'], action)
                self.assertTrue(payload['usage'])

    def test_json_flag_is_accepted_and_does_not_change_results(self):
        data = rows()
        self.assertEqual(work.queue(data, 'alice/session', ['--mine', '--json']),
                         work.queue(data, 'alice/session', ['--mine']))

    def test_help_documents_identity_of_contribution_id(self):
        payload = work.help_payload('work')
        self.assertIn('contribution comment ID', payload['output']['item_identity'])
        self.assertIn('items[].task is the native task ID', payload['output']['item_identity'])

    def test_documented_limits_are_enforced_and_named(self):
        data = rows()
        cases = ((['--limit', '0'], '--limit'), (['--limit', '101'], '--limit'),
                 (['--offset', '-1'], '--offset'),
                 (['--handoff-limit', '0'], '--handoff-limit'),
                 (['--handoff-offset', '-1'], '--handoff-offset'))
        for args, option in cases:
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, option):
                work.queue(data, 'alice/session', args)

    def test_help_limits_match_enforced_constants(self):
        payload = work.help_payload('work')
        with self.assertRaises(ValueError) as caught:
            work.queue(rows(), 'alice/session', ['--limit', str(work.WORK_LIMIT_MAX + 1)])
        self.assertIn(payload['limits']['limit'], str(caught.exception))

    def test_mistaken_flags_get_a_targeted_hint(self):
        with self.assertRaisesRegex(ValueError, 'hint: use --state'):
            work.queue(rows(), 'alice/session', ['--review-state', 'none'])
        with self.assertRaisesRegex(ValueError, 'hint: use --owner ACTOR or --mine'):
            work.queue(rows(), 'alice/session', ['--assignee', 'alice'])
        with self.assertRaisesRegex(ValueError, 'hint: work lists the queue'):
            work.queue(rows(), 'alice/session', ['--task', 'kittrial-5bb.7'])

    def test_invalid_state_names_the_option(self):
        with self.assertRaisesRegex(ValueError, '--state'):
            work.queue(rows(), 'alice/session', ['--state', 'not-a-state'])

    def test_review_read_accepts_json_consistently(self):
        lines = '\n'.join(json.dumps(row) for row in rows())
        result = work.execute(Path('.'), 'alice/session', 'review', [TASK, '--json'], {},
                              lambda argv: lines)
        self.assertEqual(result['review_state'], 'none')

    def test_review_and_handoff_help_list_json_and_help_flags(self):
        for action in ('review', 'handoff'):
            with self.subTest(action=action):
                flags = [option['flag'] for option in work.help_options(action)]
                self.assertIn('--json', flags)
                self.assertIn('-h, --help', flags)

    def test_work_output_shape_is_backward_compatible(self):
        result = work.queue(rows(), 'alice/session', ['--mine'])
        self.assertEqual(set(result), {'owner', 'total', 'items', 'next_offset', 'coverage'})
        self.assertEqual(result['total'], 1)
        self.assertEqual(set(result['items'][0]), ITEM_FIELDS)

    def test_brief_shape_is_backward_compatible(self):
        result = briefing.brief(rows(), PROJECT, TASK)
        self.assertEqual(set(result), BRIEF_FIELDS)


class CheckpointErrorContractTests(unittest.TestCase):
    """Validation errors name unknown/missing fields, index and limit."""

    def test_unknown_and_missing_fields_are_named(self):
        bad = checkpoint(rows())
        bad['extra_field'] = 1
        with self.assertRaisesRegex(ValueError, 'unknown fields: extra_field'):
            briefing.validate_checkpoint(bad, TASK)
        missing = checkpoint(rows())
        del missing['summary']
        with self.assertRaisesRegex(ValueError, 'missing fields: summary'):
            briefing.validate_checkpoint(missing, TASK)

    def test_item_text_error_names_path_and_limit(self):
        bad = checkpoint(rows(), open_items=[dict(id='i1', kind='blocker',
                                                  text='x' * 401, source='s')])
        with self.assertRaisesRegex(ValueError, r'open_items\[0\]\.text') as caught:
            briefing.validate_checkpoint(bad, TASK)
        self.assertIn('400', str(caught.exception))

    def test_unknown_item_field_is_named_with_allowed_fields(self):
        bad = checkpoint(rows(), open_items=[dict(id='i1', kind='blocker', text='x',
                                                  source='s', extra=1)])
        with self.assertRaisesRegex(ValueError, r'open_items\[0\].*unknown fields: extra'):
            briefing.validate_checkpoint(bad, TASK)

    def test_invalid_item_kind_is_named(self):
        bad = checkpoint(rows(), open_items=[dict(id='i1', kind='nope', text='x', source='s')])
        with self.assertRaisesRegex(ValueError, r'open_items\[0\]\.kind'):
            briefing.validate_checkpoint(bad, TASK)

    def test_item_list_cap_is_named(self):
        many = [dict(id='i%d' % n, kind='blocker', text='x', source='s')
                for n in range(briefing.CHECKPOINT_ITEMS_MAX + 1)]
        with self.assertRaisesRegex(ValueError, r'open_items.*%d' % briefing.CHECKPOINT_ITEMS_MAX):
            briefing.validate_checkpoint(checkpoint(rows(), open_items=many), TASK)

    def test_errors_do_not_echo_private_item_text(self):
        secret = 'PRIVATE-SECRET-' + 'z' * 420
        bad = checkpoint(rows(), open_items=[dict(id='i1', kind='blocker',
                                                  text=secret, source='s')])
        with self.assertRaises(ValueError) as caught:
            briefing.validate_checkpoint(bad, TASK)
        self.assertNotIn('PRIVATE-SECRET', str(caught.exception))


class DocumentedLimitContractTests(unittest.TestCase):
    def test_brief_item_page_limits_are_named(self):
        with self.assertRaisesRegex(ValueError, r'--items-limit must be 1\.\.10'):
            briefing.brief(rows(), PROJECT, TASK, 0, 0)
        with self.assertRaisesRegex(ValueError, r'--items-offset must be >= 0'):
            briefing.brief(rows(), PROJECT, TASK, -1, 5)

    def test_history_limits_are_named(self):
        snapshot = briefing.snapshot(rows(), PROJECT, TASK)
        with self.assertRaisesRegex(ValueError, r'History limit must be 1\.\.20'):
            briefing.history_page(snapshot, PROJECT, TASK, limit=21)
        with self.assertRaisesRegex(ValueError, r'History body budget must be 256\.\.8000'):
            briefing.history_page(snapshot, PROJECT, TASK, body_budget=100)


class NativeWarningContractTests(unittest.TestCase):
    """Warning-producing native output stays out of the success JSON."""

    def test_success_json_is_clean_and_warnings_are_separate(self):
        stdout = json.dumps({'id': 'task-1', 'comments': []}) + '\n'
        warnings = 'warning: beads.role not configured\n'
        output, stderr = native.split(completed(stdout, warnings))
        self.assertEqual(json.loads(output)['id'], 'task-1')
        self.assertNotIn('warning', output)
        self.assertEqual(stderr, warnings)

    def test_empty_warning_channel_produces_empty_stderr(self):
        output, stderr = native.split(completed('{"ok": true}', '', 0))
        self.assertEqual(json.loads(output), {'ok': True})
        self.assertEqual(stderr, '')

    def test_nonzero_is_labelled_bounded_and_does_not_dump_payload(self):
        huge = 'x' * (native.DIAGNOSTIC_LIMIT * 3)
        with self.assertRaises(ValueError) as caught:
            native.split(completed('', huge, 3))
        message = str(caught.exception)
        self.assertTrue(message.startswith('Native command failed (3)'))
        self.assertIn('[truncated]', message)
        self.assertLess(len(message), native.DIAGNOSTIC_LIMIT + 100)

    def test_native_argv_is_absolute_and_scoped(self):
        root = Path('/srv/state')
        project = Path('/srv/state/projects/p')
        scoped = native.argv(root, project, 'alice/session', ['list', '--json'])
        self.assertEqual(scoped[:3], [str(root / 'bin' / 'bd'), '--directory', str(project)])
        self.assertIn('--actor', scoped)
        unscoped = native.argv(root, project, 'alice/session', ['export', '--all'],
                               scoped=False)
        self.assertNotIn('--actor', unscoped)


class ClientStreamContractTests(unittest.TestCase):
    """The client keeps stdout JSON and stderr warnings in separate streams."""

    def invoke(self, response, action_args):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}', encoding='utf-8')
            with patch.object(sys, 'argv', ['client.py', '--config', str(config),
                                            '--project', 'example', '--actor',
                                            'alice/session', '--', *action_args]), \
                    patch.object(client, 'request', return_value=response) as request, \
                    patch('sys.stdout', new_callable=io.StringIO) as output, \
                    patch('sys.stderr', new_callable=io.StringIO) as error:
                code = client.main()
        return code, output.getvalue(), error.getvalue(), request

    def test_warning_stderr_does_not_contaminate_stdout_json(self):
        body = json.dumps({'owner': 'alice', 'total': 0, 'items': [],
                           'next_offset': None, 'coverage': 'fresh'})
        code, output, error, request = self.invoke(
            dict(stdout=body + '\n', stderr='warning: native note\n', returncode=0),
            ['work', '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['owner'], 'alice')
        self.assertNotIn('warning', output)
        self.assertEqual(error, 'warning: native note\n')
        self.assertEqual(request.call_args.args[3], ['--json'])
        self.assertEqual(request.call_args.args[4], 'work')

    def test_nonzero_error_keeps_stdout_empty_and_names_the_reason(self):
        code, output, error, _ = self.invoke(
            dict(stdout='', stderr='ValueError: --limit must be 1..100\n', returncode=2),
            ['work', '--limit', '0'])
        self.assertEqual(code, 2)
        self.assertEqual(output, '')
        self.assertIn('--limit must be 1..100', error)


class ContractDocumentationTests(unittest.TestCase):
    def test_catalog_and_readme_expose_the_contract_document(self):
        self.assertEqual(onboarding.DOCUMENTS['cli-contract'], 'docs/CLI_CONTRACT.md')
        self.assertTrue((KIT / 'docs' / 'CLI_CONTRACT.md').is_file())
        self.assertIn('docs/CLI_CONTRACT.md', (KIT / 'README.md').read_text(encoding='utf-8'))

    def test_contract_document_states_version_and_warning_stream_rule(self):
        text = (KIT / 'docs' / 'CLI_CONTRACT.md').read_text(encoding='utf-8')
        self.assertIn('cli-contract-v1', text)
        self.assertIn('never', text)
        self.assertIn('stderr', text)


if __name__ == '__main__':
    unittest.main()
