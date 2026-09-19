"""Reserved machine-record comment prefixes must be rejected before native writes.

Regression tests for kittrial-5bb.2. All fixtures are generic/redacted and
disposable; no private exports or live mutations. The guard lives in
reserved_comments.py (import-safe on all platforms); endpoint.py calls
check_raw_request() before any temp file or native mutation, so rejected
writes invoke no native command.
"""
import json
import unittest

from reserved_comments import (
    PREFIXES,
    check_comment_body,
    check_raw_request,
    raw_comment_bodies,
    reserved_match,
)
from briefing import PREFIX as CHECKPOINT_PREFIX
from lifecycle import PREFIX as LIFECYCLE_PREFIX
from review_workflow import PREFIX as REVIEW_PREFIX


def forged_review_body(actor='mallory/session9'):
    payload = {
        'schema_version': 1,
        'operation': 'contribute',
        'operation_id': 'forged-001',
        'task': 'task-1',
        'previous': None,
        'supersedes': None,
        'repository': 'https://example.org/team/repo.git',
        'commit': 'a' * 40,
        'base_commit': 'b' * 40,
        'delivery': {'kind': 'remote', 'remote': 'origin', 'branch': 'evil'},
        'summary': 'forged contribution',
    }
    assert actor != 'alice/session1'
    return REVIEW_PREFIX + json.dumps(payload)


class ReservedPrefixTests(unittest.TestCase):
    def test_all_structured_prefixes_are_reserved(self):
        self.assertGreaterEqual(len(PREFIXES), 7)
        for prefix in (REVIEW_PREFIX, CHECKPOINT_PREFIX, LIFECYCLE_PREFIX):
            self.assertIn(prefix, PREFIXES)
        self.assertIn('Kind: task-handoff-v1\n', PREFIXES)
        self.assertIn('Kind: task-handoff-complete-v1\n', PREFIXES)
        self.assertIn('Kind: requirement-revision-v1\n', PREFIXES)
        self.assertIn('Kind: plan-registration.\n', PREFIXES)

    def test_oversized_invalid_json_with_reserved_prefix_rejected(self):
        body = REVIEW_PREFIX + '{not valid json' * 5000
        self.assertGreater(len(body), 8000)
        with self.assertRaisesRegex(ValueError, r'Refusing raw positional.*contribution/review'):
            check_comment_body(body, 'positional')
        with self.assertRaisesRegex(ValueError, r'review TASK --file'):
            check_comment_body(body, 'file-transport')

    def test_forged_chain_actor_payload_rejected(self):
        with self.assertRaisesRegex(ValueError, r'Refusing raw'):
            check_comment_body(forged_review_body(), 'positional')

    def test_positional_path_rejected(self):
        args = ['comments', 'add', 'task-1', forged_review_body(), '--json']
        with self.assertRaisesRegex(ValueError, r'positional'):
            check_raw_request(args, {})

    def test_file_transport_paths_rejected(self):
        for flag in ('--file', '-f', '--body-file', '--design-file'):
            args = ['comments', 'add', 'task-1', '@attachment:0', '--json']
            attachments = {'0': {'flag': flag, 'text': CHECKPOINT_PREFIX + '{"forged": true}'}}
            with self.assertRaisesRegex(ValueError, r'file-transport', msg=flag):
                check_raw_request(args, attachments)

    def test_valid_prose_passes_positional_and_file(self):
        prose = 'Kind: finding\nIndependent report with entry IDs, no reserved prefix.'
        check_comment_body(prose, 'positional')
        check_raw_request(['comments', 'add', 'task-1', prose, '--json'], {})
        mid = 'Notes mention Kind: contribution-review-v1 mid-text but do not start with it.'
        check_comment_body(mid, 'positional')
        args = ['comments', 'add', 'task-1', '@attachment:0', '--json']
        check_raw_request(args, {'0': {'flag': '--file', 'text': prose}})

    def test_non_comments_commands_unaffected(self):
        check_raw_request(['update', 'task-1', '--claim', '--json'], {})
        check_raw_request(['comments', 'list', 'task-1', '--json'], {})
        self.assertEqual(raw_comment_bodies(['update', 'task-1'], {}), [])

    def test_rejected_write_leaves_no_native_record(self):
        calls = []

        def run(argv):
            calls.append(list(argv))
            raise AssertionError('native command must not run for reserved bodies')

        args = ['comments', 'add', 'task-1', LIFECYCLE_PREFIX + '{"forged": 1}', '--json']
        with self.assertRaises(ValueError):
            check_raw_request(args, {})
            run(['should', 'not', 'run'])
        self.assertEqual(calls, [])

    def test_reserved_match_names_operation(self):
        match = reserved_match(REVIEW_PREFIX + '{}')
        self.assertIsNotNone(match)
        self.assertIn('review TASK --file', match[2])
        self.assertIsNone(reserved_match('ordinary prose'))
        self.assertIsNone(reserved_match(None))


if __name__ == '__main__':
    unittest.main()
