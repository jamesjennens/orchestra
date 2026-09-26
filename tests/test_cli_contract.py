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
import coordination
import native
import onboarding
import work
from requirements import canonical_bytes
from test_briefing import PROJECT, TASK, checkpoint, rows

ITEM_FIELDS = {
    'task', 'title', 'owner', 'status', 'review_state', 'contribution_id', 'commit',
    'pending_review_items', 'pending_handoff_requests', 'pending_handoff_total',
    'pending_handoff_next_offset', 'lifecycle', 'lifecycle_scope',
    'lifecycle_matches_contribution', 'error',
    # Additive fields from the shared review-state projection (kittrial-5bb.24).
    'workflow_state', 'integration',
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
        self.assertIn('withheld', message)
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


class NativeFailureBoundaryTests(unittest.TestCase):
    """A nonzero native exit echoes a bounded, redacted label, never a payload."""

    SECRET = 'PRIVATE-BODY-' + 'x' * 700 + '-END'

    def message(self, stdout='', stderr='', returncode=1):
        with self.assertRaises(ValueError) as caught:
            native.split(completed(stdout, stderr, returncode))
        return str(caught.exception)

    def test_long_single_line_payload_is_withheld_not_truncated(self):
        stderr = ('Error: failed writing to /srv/state/projects/trial/.beads/db: '
                  + self.SECRET + '\n')
        message = self.message(stderr=stderr)
        self.assertTrue(message.startswith('Native command failed (1)'))
        self.assertNotIn('PRIVATE-BODY', message)
        self.assertNotIn('/srv/state', message)
        self.assertIn('withheld', message)
        self.assertIn('sha256:', message)
        self.assertLess(len(message), 300)

    def test_short_diagnostic_line_is_echoed_with_paths_removed(self):
        message = self.message(stderr='error: cannot open /srv/state/projects/trial/.beads/db: locked\n',
                               returncode=3)
        self.assertIn('cannot open', message)
        self.assertIn('<path>', message)
        self.assertIn('locked', message)
        self.assertNotIn('/srv/state', message)
        self.assertTrue(message.startswith('Native command failed (3)'))

    def test_multiline_payload_is_withheld_after_a_short_first_line(self):
        message = self.message(stderr='error: native write failed\n' + self.SECRET + '\n')
        self.assertIn('native write failed', message)
        self.assertIn('withheld', message)
        self.assertNotIn('PRIVATE-BODY', message)

    def test_failure_without_detail_is_only_the_label(self):
        self.assertEqual(self.message(returncode=9), 'Native command failed (9)')

    def test_stdout_is_used_when_stderr_is_empty(self):
        message = self.message(stdout=self.SECRET + '\n')
        self.assertNotIn('PRIVATE-BODY', message)
        self.assertIn('withheld', message)


class NativeStdoutBoundaryTests(unittest.TestCase):
    """stdout carries JSON; a stray warning line is labelled, not a decode error."""

    def test_stdout_warning_is_relabelled_and_json_stays_clean(self):
        stdout = 'Warning: native warning on stdout\n{"id": "t", "title": "T"}\n'
        output, stderr = native.split(completed(stdout, '', 0))
        self.assertEqual(json.loads(output)['id'], 't')
        self.assertNotIn('Warning', output)
        self.assertTrue(stderr.startswith('native stdout note: '))
        self.assertIn('native warning on stdout', stderr)

    def test_stdout_without_json_is_a_labelled_error_not_a_decode_error(self):
        with self.assertRaises(ValueError) as caught:
            native.split(completed('Warning: only a warning\n', '', 0))
        message = str(caught.exception)
        self.assertIn('stdout', message)
        self.assertIn('Warning: only a warning', message)
        self.assertNotIn('JSONDecodeError', message)

    def test_long_stdout_contamination_is_withheld(self):
        with self.assertRaises(ValueError) as caught:
            native.split(completed('PRIVATE-BODY-' + 'x' * 700, '', 0))
        message = str(caught.exception)
        self.assertNotIn('PRIVATE-BODY', message)
        self.assertIn('withheld', message)

    def test_json_lines_survive_a_warning_line(self):
        output, stderr = native.split(completed('{"id": 1}\nWarning: mid\n{"id": 2}\n', '', 0))
        self.assertEqual([json.loads(line)['id'] for line in output.splitlines()], [1, 2])
        self.assertIn('native stdout note: Warning: mid', stderr)

    def test_pretty_printed_json_survives_warnings_before_and_after(self):
        stdout = 'Warning: before\n{\n  "id": "t"\n}\nWarning: after\n'
        output, stderr = native.split(completed(stdout, '', 0))
        self.assertEqual(json.loads(output), {'id': 't'})
        self.assertIn('native stdout note: Warning: before', stderr)
        self.assertIn('native stdout note: Warning: after', stderr)

    def test_clean_success_keeps_stderr_warnings_verbatim(self):
        output, stderr = native.split(completed('{"id": 1}\n', 'Warning: stderr note\n', 0))
        self.assertEqual(stderr, 'Warning: stderr note\n')
        self.assertNotIn('native stdout note', stderr)


class NativeDocumentClassificationTests(unittest.TestCase):
    """One policy at the native boundary: decode the whole result document first.

    An indented document wrapped in warning lines must stay one document, JSON
    Lines is line mode only when every data line is a complete object/array, and
    scalars or log-record objects are noise - noted, or rejected when nothing
    else remains.
    """

    WARNING = 'warning: beads.role not configured\n'
    DOC = '''{
  "id": "kittrial-5bb.7",
  "title": "T",
  "description": "PRIVATE-DESC: reviewer-only text",
  "labels": [
    "review-ready",
    "private-label"
  ]
}
'''
    SHOW = '''[
  {
    "id": "kittrial-5bb.7",
    "description": "PRIVATE-DESC: reviewer-only text",
    "labels": [
      "review-ready",
      "private-label"
    ]
  }
]
'''
    OBJECT_TAIL = '''{
  "available": true,
  "holder": null,
  "waiters": [
    {"actor": "carol/session"}
  ]
}
'''
    MERGE = '''{
  "available": true,
  "holder": null,
  "task": "kittrial-5bb.7",
  "description": "PRIVATE-DESC: merge document body",
  "waiters": [
    {"actor": "alice/session"},
    {"available": true, "holder": null}
  ]
}
'''

    def split(self, stdout):
        return native.split(completed(stdout, '', 0))

    def test_indented_document_with_scalar_line_stays_one_document(self):
        output, stderr = self.split(self.WARNING + self.DOC)
        self.assertEqual(json.loads(output), json.loads(self.DOC))
        self.assertNotEqual(output.strip(), '"private-label"')
        self.assertNotIn('PRIVATE-DESC', stderr)
        self.assertEqual(stderr.count(native.NOISE_PREFIX), 1)

    def test_indented_show_document_stays_one_document(self):
        output, stderr = self.split(self.WARNING + self.SHOW)
        rows = json.loads(output)
        self.assertEqual([row['id'] for row in rows], ['kittrial-5bb.7'])
        self.assertIn('private-label', output)
        self.assertNotIn('PRIVATE-DESC', stderr)

    def test_one_line_object_tail_does_not_win_over_the_document(self):
        output, stderr = self.split(self.WARNING + self.OBJECT_TAIL)
        document = json.loads(output)
        self.assertTrue(document['available'])
        self.assertEqual(document['waiters'][0]['actor'], 'carol/session')
        self.assertNotIn('carol/session', stderr)
        self.assertEqual(stderr.count(native.NOISE_PREFIX), 1)

    def test_truncated_document_is_rejected_not_taken_as_a_fragment(self):
        truncated = ('{\n  "id": "t",\n  "labels": [\n'
                     '    "PRIVATE-LABEL"\n')
        with self.assertRaises(ValueError) as caught:
            self.split(self.WARNING + truncated)
        message = str(caught.exception)
        self.assertIn('Native stdout is not JSON', message)
        self.assertNotIn('PRIVATE-LABEL', message)

    def test_malformed_outer_document_cannot_return_an_indented_nested_object(self):
        streams = (
            '{\n  "comments": [\n'
            '    {"id": "nested", "text": "PRIVATE-COMMENT"}\n'
            'WARNING: interrupted\n',
            '[\n  {"id": "nested", "text": "PRIVATE-COMMENT"}\n'
            'WARNING: interrupted\n',
        )
        for index, stdout in enumerate(streams):
            with self.subTest(index=index):
                with self.assertRaises(ValueError) as caught:
                    self.split(stdout)
                message = str(caught.exception)
                self.assertIn('Native stdout is not JSON', message)
                self.assertNotIn('PRIVATE-COMMENT', message)

    def test_json_lines_stream_is_unchanged(self):
        output, stderr = self.split('{"id": 1}\n{"id": 2}\n')
        self.assertEqual([json.loads(line)['id'] for line in output.splitlines()],
                         [1, 2])
        self.assertEqual(stderr, '')

    def test_merge_check_document_reaches_the_caller_whole(self):
        warnings = []

        def run(argv):
            self.assertEqual(argv[:2], ['merge-slot', 'check'])
            stdout, note = native.split(completed(self.WARNING + self.MERGE, '', 0))
            if note:
                warnings.append(note)
            return stdout

        # merge-check only reads the journal path, so a missing directory is fine.
        state = coordination.apply_native({'operation': 'merge-check'},
                                          'alice/session', run,
                                          Path('merge-context-probe'))
        self.assertTrue(state['available'])
        self.assertEqual(state['task'], 'kittrial-5bb.7')
        self.assertNotIn('PRIVATE-DESC', ''.join(warnings))

    def test_two_indented_documents_are_refused_not_silently_picked(self):
        with self.assertRaises(ValueError) as caught:
            self.split(self.DOC + self.MERGE)
        message = str(caught.exception)
        self.assertIn('more than one JSON document', message)
        self.assertIn('withheld', message)
        self.assertNotIn('PRIVATE-DESC', message)
        self.assertNotIn('private-label', message)
        self.assertNotIn('kittrial-5bb.7', message)

    def test_one_line_row_beside_an_indented_document_is_refused(self):
        row = '{"id": "row-1", "text": "PRIVATE-ROW"}\n'
        for stdout in (row + self.DOC, self.DOC + row):
            with self.subTest(stdout=stdout[:12]):
                with self.assertRaises(ValueError) as caught:
                    self.split(stdout)
                message = str(caught.exception)
                self.assertIn('more than one JSON document', message)
                self.assertNotIn('PRIVATE-ROW', message)
                self.assertNotIn('PRIVATE-DESC', message)

    def test_multi_document_refusal_is_bounded(self):
        with self.assertRaises(ValueError) as caught:
            self.split(self.DOC + self.MERGE + self.SHOW)
        message = str(caught.exception)
        self.assertTrue(message.startswith(
            'Native stdout carries more than one JSON document (exit 0)'))
        self.assertIn('sha256:', message)
        self.assertLess(len(message), 300)

    def test_warning_object_beside_one_line_row_is_one_data_line_and_a_note(self):
        row = '{"id": "row-1", "text": "PRIVATE-ROW"}\n'
        notice = '{"warning": "beads.role not configured"}\n'
        for stdout in (row + notice, notice + row):
            with self.subTest(stdout=stdout[:12]):
                output, stderr = self.split(stdout)
                self.assertEqual(json.loads(output), {'id': 'row-1', 'text': 'PRIVATE-ROW'})
                self.assertEqual(len(output.strip().splitlines()), 1)
                self.assertIn(native.NOISE_PREFIX, stderr)
                self.assertIn('beads.role', stderr)
                self.assertNotIn('warning', output)

    def test_multi_line_warning_object_is_a_note_not_a_document(self):
        stdout = ('{\n  "warning": "beads.role not configured",\n  "code": 3\n}\n'
                  '{"id": "row-1"}\n')
        output, stderr = self.split(stdout)
        self.assertEqual(json.loads(output), {'id': 'row-1'})
        self.assertEqual(stderr.count(native.NOISE_PREFIX), 4)
        self.assertNotIn('beads.role', output)

    def test_whole_stream_null_is_kept_for_backward_compatibility(self):
        output, stderr = self.split('null\n')
        self.assertEqual(output, 'null\n')
        self.assertEqual(stderr, '')
        self.assertIsNone(json.loads(output))
        # coordination.py/handoff.py consume this with `or []`.
        self.assertEqual(json.loads(output) or [], [])

    def test_null_beside_a_row_is_a_note_not_a_data_row(self):
        output, stderr = self.split('null\n{"id": 1}\n')
        self.assertEqual(json.loads(output), {'id': 1})
        self.assertIn(native.NOISE_PREFIX + 'null', stderr)

    def test_lone_diagnostic_object_is_refused_as_a_documented_tightening(self):
        for stdout in ('{"message": "nothing happened"}\n',
                       '{"warning": "beads.role not configured"}\n'):
            with self.subTest(stdout=stdout):
                with self.assertRaises(ValueError) as caught:
                    self.split(stdout)
                self.assertIn('Native stdout is not JSON', str(caught.exception))

    def test_every_export_row_survives_a_warning_line(self):
        rows_text = ''.join(json.dumps({'id': number}) + '\n' for number in range(5))
        output, stderr = self.split(self.WARNING + rows_text)
        self.assertEqual([json.loads(line)['id'] for line in output.splitlines()],
                         [0, 1, 2, 3, 4])
        self.assertEqual(stderr.count(native.NOISE_PREFIX), 1)


class NativeNoiseShapeTests(unittest.TestCase):
    """Noise that happens to be valid JSON is still not result data."""

    def split(self, stdout):
        return native.split(completed(stdout, '', 0))

    def test_bare_scalar_stdout_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self.split('42\n')
        self.assertIn('Native stdout is not JSON', str(caught.exception))

    def test_scalar_noise_is_noted_not_kept_as_a_data_row(self):
        output, stderr = self.split('42\n{"id": 1}\n')
        self.assertEqual(json.loads(output), {'id': 1})
        self.assertIn(native.NOISE_PREFIX + '42', stderr)

    def test_json_log_record_is_noted_not_kept_as_a_data_row(self):
        output, stderr = self.split('{"level":"warn"}\n{"id": 1}\n')
        self.assertEqual(json.loads(output), {'id': 1})
        self.assertIn('level', stderr)
        self.assertNotIn('level', output)

    def test_lone_json_log_record_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            self.split('{"level":"warn","msg":"nothing to report"}\n')
        self.assertIn('Native stdout is not JSON', str(caught.exception))

    def test_forwarded_noise_lines_are_capped(self):
        flood = ''.join('native warning line %02d\n' % index
                        for index in range(50))
        output, stderr = self.split('{"id": 1}\n' + flood)
        self.assertEqual(json.loads(output), {'id': 1})
        self.assertEqual(stderr.count(native.NOISE_PREFIX),
                         native.NOISE_LINE_LIMIT + 1)
        self.assertIn('withheld', stderr)
        self.assertLess(len(stderr), 1000)


class RedactionBoundaryTests(unittest.TestCase):
    """The echoed diagnostic line drops whole path-shaped tokens."""

    def test_quoted_windows_path_with_spaces_is_removed_whole(self):
        value = native.redact(
            r'error: cannot open "C:\Users\James Smith\private notes\db.txt": locked')
        self.assertNotIn('Smith', value)
        self.assertNotIn('notes', value)
        self.assertNotIn('private', value)
        self.assertIn('<path>', value)
        self.assertIn('locked', value)

    def test_unquoted_windows_path_with_spaces_keeps_prose(self):
        value = native.redact(
            r'error: cannot open C:\Users\James Smith\private notes\db.txt: locked')
        self.assertNotIn('Smith', value)
        self.assertNotIn('notes', value)
        self.assertIn('locked', value)

    def test_relative_path_prefix_is_removed(self):
        self.assertEqual(native.redact('error: cannot read data/private/tok.txt'),
                         'error: cannot read <path>')

    def test_actor_like_two_segment_token_is_left_alone(self):
        text = 'error: actor alice/session is not the current owner'
        self.assertEqual(native.redact(text), text)

    def test_file_uri_is_removed_whole(self):
        self.assertEqual(
            native.redact('error: cannot read file:///c:/Users/James/private/tok.txt'),
            'error: cannot read <path>')

    def test_posix_absolute_path_is_removed(self):
        value = native.redact(
            'error: cannot open /srv/state/projects/trial/.beads/db: locked')
        self.assertNotIn('/srv/state', value)
        self.assertIn('<path>', value)
        self.assertIn('locked', value)

    def test_unquoted_windows_path_with_trailing_space_segment_is_removed_whole(self):
        value = native.redact(
            r'error: cannot open C:\Users\James Smith\private notes: locked')
        self.assertNotIn('Smith', value)
        self.assertNotIn('notes', value)
        self.assertNotIn('private', value)
        self.assertIn('<path>', value)
        for word in ('cannot', 'open', 'locked'):
            self.assertIn(word, value)

    def test_unquoted_windows_path_prose_survives_when_the_path_has_no_space(self):
        value = native.redact(r'error: cannot open C:\db file is locked')
        self.assertEqual(value, 'error: cannot open <path> file is locked')

    def test_tilde_path_loses_the_tilde_and_every_segment(self):
        self.assertEqual(native.redact('error: cannot read ~/x'),
                         'error: cannot read <path>')
        self.assertEqual(native.redact(r'error: cannot read ~\private\tok.txt'),
                         'error: cannot read <path>')

    def test_space_broken_tilde_path_loses_every_segment(self):
        value = native.redact('error: cannot read ~/private notes/tok.txt')
        for token in ('private', 'notes', 'tok.txt', '~'):
            self.assertNotIn(token, value)
        self.assertIn('<path>', value)


class CheckpointFieldNameBoundTests(unittest.TestCase):
    """Caller-supplied field names in errors are capped in length and count."""

    BIG_KEY = 'PRIVATE-KEY-' + 'k' * 5000

    def message(self, payload):
        with self.assertRaises(ValueError) as caught:
            briefing.validate_checkpoint(payload, TASK)
        return str(caught.exception)

    def test_oversized_top_level_key_is_capped(self):
        bad = checkpoint(rows())
        bad[self.BIG_KEY] = 1
        message = self.message(bad)
        self.assertIn('unknown fields:', message)
        self.assertNotIn(self.BIG_KEY, message)
        self.assertIn(self.BIG_KEY[:briefing.FIELD_NAME_LIMIT] + '...', message)
        self.assertLess(len(message), 300)

    def test_oversized_item_key_is_capped(self):
        bad = checkpoint(rows(), open_items=[dict(id='i1', kind='blocker', text='x',
                                                  source='s', **{self.BIG_KEY: 1})])
        message = self.message(bad)
        self.assertIn('open_items[0]', message)
        self.assertNotIn(self.BIG_KEY, message)
        self.assertIn(self.BIG_KEY[:briefing.FIELD_NAME_LIMIT] + '...', message)
        self.assertLess(len(message), 300)

    def test_many_unknown_keys_are_count_capped(self):
        bad = checkpoint(rows())
        for number in range(30):
            bad['extra-%02d' % number] = 1
        message = self.message(bad)
        self.assertIn('unknown fields:', message)
        self.assertIn('(+%d more)' % (30 - briefing.FIELD_NAME_COUNT), message)
        self.assertEqual(message.count('extra-'), briefing.FIELD_NAME_COUNT)
        self.assertLess(len(message), 400)

    def test_missing_fields_stay_named_and_bounded(self):
        bad = checkpoint(rows())
        for field in ('summary', 'next_action', 'open_items', 'resolved'):
            del bad[field]
        message = self.message(bad)
        self.assertIn('missing fields:', message)
        self.assertIn('open_items', message)
        self.assertLess(len(message), 300)

    def test_short_unknown_field_is_still_named(self):
        bad = checkpoint(rows())
        bad['extra_field'] = 1
        self.assertIn('unknown fields: extra_field', self.message(bad))


def fail_run(argv):
    raise AssertionError('help must not invoke the native command runner: %r' % (argv,))


class HelpConsistencyTests(unittest.TestCase):
    """Help is recognised uniformly and never runs a native command."""

    def test_help_after_a_positional_or_another_flag(self):
        for action, args in (('review', [TASK, '--help']), ('handoff', ['--json', '--help']),
                             ('handoff', [TASK, '-h']), ('review', ['--json', TASK, '--help'])):
            with self.subTest(action=action, args=args):
                payload = work.execute(Path('.'), 'alice/session', action, args, {}, fail_run)
                self.assertEqual(payload['command'], action)
                self.assertTrue(payload['usage'].startswith(action))

    def test_work_help_after_options_is_side_effect_free(self):
        calls = []

        def run(argv):
            calls.append(argv)
            raise AssertionError('work help must not run a native command')

        payload = work.execute(Path('.'), 'alice/session', 'work',
                               ['--state', 'none', '-h'], {}, run)
        self.assertEqual(calls, [])
        self.assertEqual(payload['command'], 'work')

    def test_option_value_h_is_still_an_option_error(self):
        with self.assertRaises(ValueError) as caught:
            work.execute(Path('.'), 'alice/session', 'work', ['--owner', '-h'], {},
                         lambda argv: '')
        message = str(caught.exception)
        self.assertIn('--owner', message)
        self.assertNotIn('"command"', message)

    def test_brief_history_and_checkpoint_expose_help(self):
        for action in ('brief', 'history', 'checkpoint'):
            with self.subTest(action=action):
                output = briefing.execute(Path('.'), Path('.'), 'example', 'alice/session',
                                          action, [TASK, '--help'], {}, fail_run)
                payload = json.loads(output)
                self.assertEqual(payload['command'], action)
                self.assertTrue(payload['usage'].startswith(action))
                self.assertIn('limits', payload)

    def test_help_options_list_help_and_json_for_every_command(self):
        for action in ('work', 'review', 'handoff', 'brief', 'history', 'checkpoint'):
            with self.subTest(action=action):
                flags = [option['flag'] for option in work.help_payload(action)['options']]
                self.assertIn('-h, --help', flags)
                self.assertIn('--json', flags)

    def test_brief_limit_mistake_names_the_correct_option(self):
        with self.assertRaisesRegex(ValueError, 'hint: use --items-limit'):
            briefing.parse_args('brief', [TASK, '--limit', '3'])
        with self.assertRaisesRegex(ValueError, 'hint: use --items-offset'):
            briefing.parse_args('brief', [TASK, '--offset', '2'])
        self.assertEqual(briefing.parse_args('history', [TASK, '--limit', '3']).limit, 3)

    def test_checkpoint_help_documents_the_field_name_bound(self):
        payload = work.help_payload('checkpoint')
        self.assertIn('%d names' % briefing.FIELD_NAME_COUNT,
                      payload['limits']['error field names'])


class CheckpointJsonFlagTests(unittest.TestCase):
    """`checkpoint --json` is accepted in any position, and help says so."""

    def run_checkpoint(self, args):
        data = rows()
        text = canonical_bytes(checkpoint(data)).decode()
        calls = []

        def run(argv):
            calls.append(argv)
            if argv[:1] == ['export']:
                return '\n'.join(json.dumps(row) for row in data) + '\n'
            if argv[:2] == ['comments', 'add']:
                return json.dumps({'id': 'cp-new'}) + '\n'
            raise AssertionError('unexpected native command %r' % (argv,))

        output = briefing.execute(KIT, KIT, PROJECT, 'alice/session', 'checkpoint',
                                  list(args), {'0': {'flag': '--file', 'text': text}}, run)
        return json.loads(output), calls

    def test_json_is_accepted_in_every_position(self):
        positions = ([TASK, '@attachment:0', '--json'],
                     ['--json', TASK, '@attachment:0'],
                     [TASK, '--json', '@attachment:0'])
        for args in positions:
            with self.subTest(args=args):
                result, calls = self.run_checkpoint(args)
                self.assertEqual(result, {'comment_id': 'cp-new', 'reconciled': False})
                self.assertEqual(calls[-1][:2], ['comments', 'add'])

    def test_json_does_not_change_the_saved_checkpoint(self):
        self.assertEqual(self.run_checkpoint([TASK, '@attachment:0'])[0],
                         self.run_checkpoint([TASK, '@attachment:0', '--json'])[0])

    def test_json_alone_still_names_the_usable_form(self):
        with self.assertRaisesRegex(ValueError, r'--file checkpoint\.json'):
            briefing.execute(KIT, KIT, PROJECT, 'alice/session', 'checkpoint',
                             [TASK, '--json'], {}, fail_run)

    def test_help_usage_and_option_list_agree_with_the_behaviour(self):
        payload = work.help_payload('checkpoint')
        self.assertIn('--json', payload['usage'])
        self.assertIn('[--json]', payload['usage'])
        option = next(entry for entry in payload['options'] if entry['flag'] == '--json')
        self.assertIn('any position', option['description'])
        self.assertTrue(any('--json' in note for note in payload['notes']))
        # The documented positions are exactly the ones the command accepts.
        self.run_checkpoint([TASK, '@attachment:0', '--json'])


class RawCommentsPassthroughContractTests(unittest.TestCase):
    """`comments list TASK --json` stays a raw, parseable `bd` passthrough."""

    def invoke(self, response, action_args):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}', encoding='utf-8')
            with patch.object(sys, 'argv', ['client.py', '--config', str(config),
                                            '--project', 'example', '--actor',
                                            'alice/session', '--', *action_args]), \
                    patch.object(client, 'request', return_value=response) as request, \
                    patch('sys.stdout', new_callable=io.StringIO) as output, \
                    patch('sys.stderr', new_callable=io.StringIO):
                code = client.main()
        return code, output.getvalue(), request

    def test_comments_list_json_is_forwarded_verbatim_and_parses(self):
        body = json.dumps([{'id': '1', 'text': 'a comment'}])
        code, output, request = self.invoke(
            dict(stdout=body + '\n', stderr='', returncode=0),
            ['comments', 'list', TASK, '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(request.call_args.args[3], ['comments', 'list', TASK, '--json'])
        self.assertEqual(request.call_args.args[4], 'bd')
        self.assertEqual(json.loads(output), [{'id': '1', 'text': 'a comment'}])

    def test_comments_task_json_keeps_the_flag_at_the_end(self):
        code, output, request = self.invoke(
            dict(stdout='[]\n', stderr='', returncode=0), ['comments', TASK, '--json'])
        self.assertEqual(code, 0)
        self.assertEqual(request.call_args.args[3], ['comments', TASK, '--json'])
        self.assertEqual(json.loads(output), [])


class ClientCaptureTests(unittest.TestCase):
    """`--out` is a client-owned UTF-8 capture that no shell can re-encode."""

    def invoke(self, response, action_args, out=False):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / 'config.json'
            config.write_text('{}', encoding='utf-8')
            destination = Path(directory) / 'result.json'
            argv = ['client.py', '--config', str(config), '--project', 'example',
                    '--actor', 'alice/session', *(['--out', str(destination)] if out else []),
                    '--', *action_args]
            with patch.object(sys, 'argv', argv), \
                    patch.object(client, 'request', return_value=response), \
                    patch('sys.stdout', new_callable=io.StringIO) as output, \
                    patch('sys.stderr', new_callable=io.StringIO) as error:
                code = client.main()
            written = destination.read_bytes() if destination.exists() else None
            return code, output.getvalue(), error.getvalue(), written

    def test_out_writes_utf8_without_bom_and_leaves_stdout_empty(self):
        title = 'unicode \u00fcn\u00efcode \u2713'
        body = json.dumps({'title': title, 'total': 1}, ensure_ascii=False)
        code, output, error, written = self.invoke(
            dict(stdout=body + '\n', stderr='warning: note\n', returncode=0),
            ['work', '--json'], out=True)
        self.assertEqual(code, 0)
        self.assertEqual(output, '')
        self.assertEqual(error, 'warning: note\n')
        self.assertIsNotNone(written)
        self.assertFalse(written.startswith(b'\xef\xbb\xbf'))
        self.assertEqual(written.decode('utf-8'), body + '\n')
        self.assertEqual(json.loads(written.decode('utf-8'))['title'], title)

    def test_out_on_failure_writes_no_file_and_keeps_the_error(self):
        code, output, error, written = self.invoke(
            dict(stdout='', stderr='ValueError: --limit must be 1..100\n', returncode=2),
            ['work', '--limit', '0'], out=True)
        self.assertEqual(code, 2)
        self.assertEqual(output, '')
        self.assertIn('--limit must be 1..100', error)
        self.assertIsNone(written)

    def test_without_out_the_client_still_writes_stdout(self):
        code, output, error, written = self.invoke(
            dict(stdout='{"owner": "alice"}\n', stderr='', returncode=0), ['work'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['owner'], 'alice')
        self.assertIsNone(written)


class CaptureEncodingPolicyTests(unittest.TestCase):
    """The documented BOM mapping decodes every PowerShell capture variant."""

    def test_documented_bom_mapping(self):
        payload = {'title': 'unicode \u00fcn\u00efcode \u2713'}
        text = json.dumps(payload, ensure_ascii=False)
        cases = ((b'\xff\xfe' + text.encode('utf-16-le'), 'utf-16'),
                 (b'\xef\xbb\xbf' + text.encode('utf-8'), 'utf-8-sig'),
                 (text.encode('utf-8'), 'utf-8'))
        for raw, expected in cases:
            with self.subTest(expected=expected):
                encoding = ('utf-16' if raw[:2] in (b'\xff\xfe', b'\xfe\xff')
                            else 'utf-8-sig' if raw[:3] == b'\xef\xbb\xbf' else 'utf-8')
                self.assertEqual(encoding, expected)
                self.assertEqual(json.loads(raw.decode(encoding)), payload)


class RevisionContractDocumentationTests(unittest.TestCase):
    """The contract states the reviewed native boundary and the capture paths."""

    def text(self):
        return (KIT / 'docs' / 'CLI_CONTRACT.md').read_text(encoding='utf-8')

    def test_native_boundary_is_documented(self):
        text = self.text()
        for phrase in ('[native output withheld', 'native stdout note:', 'sha256:',
                       '<path>', 'Native stdout is not JSON'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_raw_passthrough_is_documented_as_intentional(self):
        text = self.text()
        self.assertIn('Raw `bd` passthrough is intentionally unchanged', text)
        self.assertIn('Native stderr on success is forwarded verbatim', text)

    def test_field_name_bound_is_documented(self):
        self.assertIn('| any checkpoint error | listed unknown/missing field names |',
                      self.text())

    def test_help_rule_and_brief_hint_are_documented(self):
        text = self.text()
        self.assertIn('`work --owner -h`', text)
        self.assertIn('hint: use --items-limit for the unresolved-item page size', text)

    def test_capture_paths_and_bom_detection_are_documented(self):
        text = self.text()
        for phrase in ('FF FE', 'EF BB BF', 'utf-8-sig', 'cmd /c', '--out FILE'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_multi_document_and_whole_stream_null_policy_are_documented(self):
        text = self.text()
        for phrase in ('at most one data document per stream',
                       'Native stdout carries more than one JSON document',
                       'whole-stream `null`',
                       'warning/notice shape'):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, text)

    def test_checkpoint_json_flag_is_documented_with_its_usage(self):
        text = self.text()
        self.assertIn('checkpoint TASK --file checkpoint.json [--json]', text)
        self.assertIn('`--json` is accepted in any position', text)

    def test_tilde_and_space_broken_path_redaction_are_documented(self):
        text = self.text()
        self.assertIn('home-relative `~/`/`~\\` tokens', text)
        self.assertIn('`~/x` becomes', text)


if __name__ == '__main__':
    unittest.main()
