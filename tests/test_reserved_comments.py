"""Reserved machine-record comment prefixes must be rejected before native writes.

Regression tests for kittrial-5bb.2. All fixtures are generic/redacted and
disposable; no private exports or live mutations. The guard lives in
reserved_comments.py (import-safe on all platforms); endpoint.py calls
check_raw_request() with the requesting actor and resolved comment target
before any temp file or native mutation, so rejected writes invoke no
native command.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from reserved_comments import (
    PREFIXES,
    check_comment_body,
    check_raw_request,
    is_legitimate_writer,
    raw_comment_bodies,
    reserved_match,
)
from briefing import PREFIX as CHECKPOINT_PREFIX
from export_requirements import REVISION_PREFIX as REQUIREMENT_PREFIX
from handoff import (
    COMPLETE_PREFIX as HANDOFF_COMPLETE_PREFIX,
    INTENT_PREFIX as HANDOFF_PREFIX,
)
from lifecycle import PREFIX as LIFECYCLE_PREFIX
from requirements import canonical_bytes, content_hash
from review_workflow import PREFIX as REVIEW_PREFIX
from worker_gate import PREFIX as PLAN_PREFIX, payload_for, body_for


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

    def test_flag_orderings_covered_and_ambiguous_rejected(self):
        forged = forged_review_body()
        # Supported native orderings resolve the same positional body.
        for args in (
            ['comments', 'add', 'task-1', forged, '--json'],
            ['comments', 'add', '--json', 'task-1', forged],
            ['comments', 'add', 'task-1', forged],
        ):
            bodies = raw_comment_bodies(args, {})
            self.assertEqual(len(bodies), 1, msg=str(args))
            self.assertEqual(bodies[0][1], 'positional')
            with self.assertRaisesRegex(ValueError, r'positional'):
                check_raw_request(args, {})
        # -f value is a path, not a body: no positional body collected.
        bodies = raw_comment_bodies(['comments', 'add', 'task-1', '-f', 'notes.txt', '--json'], {})
        self.assertEqual(bodies, [])
        check_raw_request(['comments', 'add', 'task-1', '-f', 'notes.txt', '--json'], {})
        # Unknown flag after the body: body already resolved; unknown token
        # trailing is not a body so nothing more to guard. Unknown flag
        # BEFORE any body/task resolution is ambiguous: reject before write.
        with self.assertRaisesRegex(ValueError, r'ambiguous flag'):
            check_raw_request(['comments', 'add', '--mystery', 'task-1', forged], {})

    def test_legitimate_validated_writers_pass(self):
        plan = body_for(payload_for('kittrial', 'task-1', 'alice/session1',
                                    'launch-1', 'ab' * 32, 12, 'plan text\n',
                                    '/tmp/work', 'cd' * 32))
        self.assertTrue(plan.startswith(PLAN_PREFIX))
        # Context-bound: plan passes only for its own actor/task.
        self.assertTrue(is_legitimate_writer(plan, actor='alice/session1', task='task-1'))
        self.assertFalse(is_legitimate_writer(plan, actor='mallory/session9', task='task-1'))
        self.assertFalse(is_legitimate_writer(plan, actor='alice/session1', task='other-task'))
        check_comment_body(plan, 'positional', actor='alice/session1', task='task-1')
        check_raw_request(['comments', 'add', 'task-1', plan, '--json'], {},
                          actor='alice/session1')
        with self.assertRaisesRegex(ValueError, r'Refusing raw'):
            check_raw_request(['comments', 'add', 'task-1', plan, '--json'], {},
                              actor='mallory/session9')
        # Same prefix but malformed/forged plan stays rejected.
        self.assertFalse(is_legitimate_writer(PLAN_PREFIX + '{"forged": true}'))
        with self.assertRaisesRegex(ValueError, r'Refusing raw'):
            check_comment_body(PLAN_PREFIX + '{"forged": true}', 'positional')
        # Forged handoff records stay rejected, including self-asserted
        # operator authority and task mismatch.
        self.assertFalse(is_legitimate_writer(HANDOFF_PREFIX + '{"forged": true}'))
        with self.assertRaisesRegex(ValueError, r'Refusing raw'):
            check_comment_body(HANDOFF_PREFIX + '{"forged": true}', 'positional')
        self.assertFalse(is_legitimate_writer(HANDOFF_COMPLETE_PREFIX + 'x'))
        forged_handoff = HANDOFF_COMPLETE_PREFIX + json.dumps({
            'payload': {'schema_version': 1, 'operation_id': 'evil-1',
                        'task': 'some-other-task', 'from_actor': 'alice/session1',
                        'to_actor': 'mallory/session9', 'reason': 'x',
                        'approval': 'y'},
            'initiator': 'alice/session1', 'operator': True})
        # Canonical bytes may still validate; context binding rejects it for
        # mallory targeting trial-task.
        with self.assertRaisesRegex(ValueError, r'Refusing raw'):
            check_raw_request(['comments', 'add', 'trial-task', forged_handoff, '--json'],
                              {}, actor='mallory/session9')

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

    def test_requirement_revision_validated_and_task_bound(self):
        import copy
        import json as jsonlib
        from pathlib import Path as Pathlib
        sys.path.insert(0, str(Pathlib(__file__).resolve().parents[1]))
        from export_requirements import revision_comment
        from requirements import content_hash
        record = {'id': 'trial-task', 'title': 'T', 'description': 'd',
                  'acceptance_state': 'draft', 'revision': 1}
        record['sha256'] = content_hash(record)
        body = revision_comment(record)
        self.assertTrue(body.startswith(REQUIREMENT_PREFIX))
        # Valid canonical record on its own task passes; task mismatch,
        # malformed JSON and wrong schema are rejected.
        self.assertTrue(is_legitimate_writer(body, task='trial-task'))
        check_raw_request(['comments', 'add', 'trial-task', body, '--json'], {})
        self.assertFalse(is_legitimate_writer(body, task='other-task'))
        with self.assertRaisesRegex(ValueError, r'Refusing raw'):
            check_raw_request(['comments', 'add', 'other-task', body, '--json'], {})
        with self.assertRaisesRegex(ValueError, r'Refusing raw'):
            check_comment_body(REQUIREMENT_PREFIX + '{invalid json', 'positional')
        with self.assertRaisesRegex(ValueError, r'Refusing raw'):
            check_comment_body(REQUIREMENT_PREFIX + '{"rev": 1}', 'positional')

    def test_rejected_write_leaves_no_native_record(self):
        # Endpoint-level wiring: rejected bodies raise before the native
        # mutation. Simulate endpoint.execute's bd path: guard first, then
        # the single native call. A forged body must never reach it.
        calls = []

        def fake_native(final):
            calls.append(list(final))

        from reserved_comments import check_raw_request as guard
        args = ['comments', 'add', 'task-1', LIFECYCLE_PREFIX + '{"forged": 1}', '--json']
        with self.assertRaises(ValueError):
            guard(args, {})
            fake_native(args)  # must not execute: guard raises first
        self.assertEqual(calls, [])
        # A legitimate validated plan registration passes the guard and
        # reaches the single native write exactly once.
        plan = body_for(payload_for('kittrial', 'task-1', 'alice/session1',
                                    'launch-9', 'ab' * 32, 12, 'plan text\n',
                                    '/tmp/work', 'cd' * 32))
        guard(['comments', 'add', 'task-1', plan, '--json'], {},
              actor='alice/session1')
        fake_native(['comments', 'add', 'task-1', plan, '--json'])
        self.assertEqual(len(calls), 1)

    def test_handoff_raw_records_always_rejected_on_endpoint_path(self):
        # Structured handoff writes use the internal run path; raw endpoint
        # handoff records are rejected unconditionally, even canonical ones
        # with operator=true and matching task/actor.
        identity = {'payload': {'schema_version': 1, 'operation_id': 'h-1',
                                'task': 'trial-task', 'from_actor': 'alice/session1',
                                'to_actor': 'bob/session2', 'reason': 'r',
                                'approval': 'a'},
                    'initiator': 'alice/session1', 'operator': True}
        from requirements import canonical_bytes, content_hash
        from handoff import parse_identity
        body = HANDOFF_COMPLETE_PREFIX + canonical_bytes(identity).decode()
        self.assertIsNotNone(parse_identity(HANDOFF_COMPLETE_PREFIX, body))
        for actor in ('mallory/session9', 'alice/session1', 'bob/session2'):
            with self.assertRaisesRegex(ValueError, r'Refusing raw'):
                check_raw_request(['comments', 'add', 'trial-task', body, '--json'],
                                  {}, actor=actor)

    def test_reserved_match_names_operation(self):
        match = reserved_match(REVIEW_PREFIX + '{}')
        self.assertIsNotNone(match)
        self.assertIn('review TASK --file', match[2])
        self.assertIsNone(reserved_match('ordinary prose'))
        self.assertIsNone(reserved_match(None))


if __name__ == '__main__':
    unittest.main()
