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
    comment_target,
    first_reserved_label,
    is_legitimate_writer,
    label_guard_request,
    operator_only_flag,
    operator_only_in_args,
    raw_comment_bodies,
    raw_file_flag_in_args,
    reserved_label_in_args,
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


class StructuralFlagOrderTests(unittest.TestCase):
    """kittrial-5bb.23: global flags before `add` must not bypass the guard.

    Real bd 1.2.2 accepts `comments --json add T BODY`, `comments -q add ...`,
    `comments -v add ...` and `comments --sandbox add ...`. Before the fix the
    guard required args[1] == 'add', so the body was invisible and a forged
    reserved machine record was written natively.
    """

    ORDERINGS = (
        ['comments', '--json', 'add', 'task-1', '@BODY@', '--json'],
        ['comments', '-q', 'add', 'task-1', '@BODY@'],
        ['comments', '--quiet', 'add', 'task-1', '@BODY@'],
        ['comments', '-v', 'add', 'task-1', '@BODY@'],
        ['comments', '--verbose', 'add', 'task-1', '@BODY@'],
        ['comments', '--sandbox', 'add', 'task-1', '@BODY@'],
        ['comments', '--global', 'add', 'task-1', '@BODY@'],
        ['comments', '--readonly', 'add', 'task-1', '@BODY@'],
        ['comments', '-qv', 'add', 'task-1', '@BODY@'],
        ['comments', '--json', '-q', 'add', 'task-1', '@BODY@'],
    )

    def _args(self, template, body):
        return [body if token == '@BODY@' else token for token in template]

    def test_global_flags_before_add_resolve_target_and_body(self):
        for template in self.ORDERINGS:
            args = self._args(template, 'ordinary prose')
            self.assertEqual(comment_target(args), 'task-1', msg=str(args))
            bodies = raw_comment_bodies(args, {})
            self.assertEqual(len(bodies), 1, msg=str(args))
            self.assertEqual(bodies[0], ('ordinary prose', 'positional'), msg=str(args))
            # Ordinary prose remains valid in every accepted ordering.
            check_raw_request(args, {})

    def test_every_reserved_prefix_refused_with_flags_before_add(self):
        for prefix in PREFIXES:
            forged = prefix + '{"forged": true}'
            for template in self.ORDERINGS:
                args = self._args(template, forged)
                with self.subTest(prefix=prefix[:28], args=str(args)):
                    with self.assertRaisesRegex(ValueError, r'Refusing raw'):
                        check_raw_request(args, {}, actor='mallory/session9')

    def test_file_transport_with_flags_before_add_refused(self):
        for template in (
            ['comments', '--json', 'add', 'task-1', '@attachment:0', '--json'],
            ['comments', '-q', 'add', 'task-1', '@attachment:0'],
            ['comments', '--sandbox', 'add', 'task-1', '@attachment:0'],
        ):
            attachments = {'0': {'flag': '--file',
                                 'text': REVIEW_PREFIX + '{"forged": true}'}}
            with self.assertRaisesRegex(ValueError, r'file-transport'):
                check_raw_request(template, attachments)

    def test_global_value_flags_before_add_are_consumed(self):
        # `--actor` is operator-only at the endpoint but the structural parser
        # must still consume its value so the body is not mistaken for a value.
        args = ['comments', '--actor', 'operator-x', 'add', 'task-1', 'prose']
        self.assertEqual(comment_target(args), 'task-1')
        self.assertEqual(raw_comment_bodies(args, {}), [('prose', 'positional')])
        args = ['comments', '--dolt-auto-commit', 'on', 'add', 'task-1', 'prose']
        self.assertEqual(comment_target(args), 'task-1')
        args = ['comments', '-C', '/tmp/work', 'add', 'task-1', 'prose']
        self.assertEqual(comment_target(args), 'task-1')

    def test_unknown_flag_is_ambiguous_and_fails_closed(self):
        forged = forged_review_body()
        for args in (
            ['comments', 'add', '--mystery', 'task-1', forged],
            ['comments', '--mystery', 'add', 'task-1', forged],
            ['comments', '--mystery', 'task-1', forged],
        ):
            with self.subTest(args=str(args)):
                with self.assertRaisesRegex(ValueError, r'ambiguous flag'):
                    check_raw_request(args, {})

    def test_double_dash_still_guards_reserved_body(self):
        forged = forged_review_body()
        with self.assertRaisesRegex(ValueError, r'Refusing raw'):
            check_raw_request(['comments', 'add', 'task-1', '--', forged], {})

    def test_non_add_comments_with_unknown_flag_not_blocked(self):
        # Unknown flags on a non-`add` comments subcommand cannot hide a write
        # body; the guard stays a no-op.
        check_raw_request(['comments', 'list', '--mystery', 'task-1', '--json'], {})
        self.assertEqual(
            raw_comment_bodies(['comments', 'list', '--mystery', 'task-1'], {}), [])


class OperatorOnlyFlagTests(unittest.TestCase):
    """kittrial-5bb.23: identity/connection flags in every pflag spelling."""

    def test_author_and_actor_spellings_rejected(self):
        for token in ('-a', '-aoperator-x', '-a=operator-x', '--author',
                      '--author=operator-x', '--actor', '--actor=operator-x',
                      '-qa', '-va', '-ha'):
            with self.subTest(token=token):
                self.assertIsNotNone(operator_only_flag(token), msg=token)

    def test_connection_and_file_spellings_rejected(self):
        for token in ('-C', '-C/tmp/work', '-C=/tmp/work', '--directory',
                      '--directory=/tmp', '--db', '--db=x', '--repo',
                      '--global', '--profile', '--graph', '--config',
                      '--metadata', '-f', '-fnotes.txt', '-f=notes.txt',
                      '--file', '--file=notes.txt', '--body-file',
                      '--design-file'):
            with self.subTest(token=token):
                self.assertIsNotNone(operator_only_flag(token), msg=token)

    def test_permitted_and_ordinary_tokens_not_flagged(self):
        for token in ('-q', '-v', '-h', '-qv', '--json', '--quiet',
                      '--verbose', '--sandbox', '--readonly',
                      '--ignore-schema-skew', '--dolt-auto-commit',
                      '@attachment:0', 'task-1', 'prose', '-', '--', None, 7):
            with self.subTest(token=token):
                self.assertIsNone(operator_only_flag(token), msg=repr(token))

    def test_joined_value_flag_is_not_split_into_author(self):
        # `-f` consumes the joined value: `-fa` is a file named "a", not -a.
        self.assertEqual(operator_only_flag('-fabc.txt'), '--file')
        self.assertEqual(operator_only_flag('-Cdata'), '--directory')

    def test_operator_only_in_args_rejects_first_offender(self):
        # This is the exact predicate endpoint.execute applies to the bd path.
        cases = (
            (['comments', 'add', 'task-1', 'hello', '-a', 'operator-x'], '--author'),
            (['comments', 'add', 'task-1', 'hello', '-aoperator-x'], '--author'),
            (['comments', 'add', 'task-1', 'hello', '-a=operator-x'], '--author'),
            (['comments', 'add', 'task-1', 'hello', '--author=operator-x'], '--author'),
            (['comments', 'add', 'task-1', 'hello', '--actor=operator-x'], '--actor'),
            (['comments', 'add', 'task-1', 'hello', '-C/tmp'], '--directory'),
            (['comments', 'add', 'task-1', 'hello', '-fid_rsa'], '--file'),
            (['comments', 'list', 'task-1', '--db=x'], '--db'),
        )
        for args, expected in cases:
            with self.subTest(args=str(args)):
                self.assertEqual(operator_only_in_args(args), expected)
        self.assertIsNone(operator_only_in_args(
            ['comments', 'add', 'task-1', 'hello', '--json', '-q']))
        # `--` ends flag parsing, so a body operand is not treated as a flag.
        self.assertIsNone(operator_only_in_args(
            ['comments', 'add', 'task-1', '--', '-aoperator-zzz']))
        self.assertIsNone(operator_only_in_args(None))


class AttachmentBeforeSubcommandTests(unittest.TestCase):
    """kittrial-5bb.23 P1: an attachment token before the subcommand bypassed
    the guard. The endpoint expands `@attachment:k` into its file flag in
    place, so `comments @attachment:k add T` becomes `comments --file PATH add
    T`, which cobra accepts; `_comments_parts` used to take the attachment as
    the subcommand and collected no body. That ordering must fail closed while
    attachments after the subcommand keep being inspected.
    """

    def test_attachment_before_add_refused_in_every_spelling(self):
        forged = forged_review_body()
        cases = (
            (['comments', '@attachment:k', 'add', 'task-1'],
             {'k': {'flag': '--file', 'text': forged}}),
            (['comments', '@attachment:k', 'add', 'task-1'],
             {'k': {'flag': '-f', 'text': forged}}),
            (['comments', '--json', '@attachment:k', 'add', 'task-1'],
             {'k': {'flag': '--file', 'text': forged}}),
            (['comments', '-q', '@attachment:k', 'add', 'task-1'],
             {'k': {'flag': '--file', 'text': forged}}),
            (['comments', '--json', '-q', '@attachment:k', 'add', 'task-1'],
             {'k': {'flag': '-f', 'text': forged}}),
        )
        for args, attachments in cases:
            with self.subTest(args=str(args)):
                with self.assertRaisesRegex(ValueError, r'attachment'):
                    check_raw_request(args, attachments)
                with self.assertRaisesRegex(ValueError, r'attachment'):
                    comment_target(args)
                # raw_comment_bodies must refuse, never silently return [].
                with self.assertRaisesRegex(ValueError, r'attachment'):
                    raw_comment_bodies(args, attachments)

    def test_attachment_after_add_still_guarded(self):
        forged = forged_review_body()
        args = ['comments', 'add', 'task-1', '@attachment:k']
        attachments = {'k': {'flag': '--file', 'text': forged}}
        with self.assertRaisesRegex(ValueError, r'file-transport'):
            check_raw_request(args, attachments)
        self.assertEqual(comment_target(args), 'task-1')
        self.assertEqual(raw_comment_bodies(args, attachments),
                         [(forged, 'file-transport')])
        # Ordinary attachment prose after the subcommand stays accepted.
        check_raw_request(args, {'k': {'flag': '--file', 'text': 'plain prose'}})

    def test_attachment_before_add_reaches_no_native_write(self):
        calls = []

        def native(final, attachments):
            calls.append((list(final), dict(attachments)))

        args = ['comments', '@attachment:k', 'add', 'task-1']
        attachments = {'k': {'flag': '--file', 'text': forged_review_body()}}
        with self.assertRaises(ValueError):
            check_raw_request(args, attachments)
            native(args, attachments)  # must not execute: guard raises first
        self.assertEqual(calls, [])


class ShortFlagClusterTests(unittest.TestCase):
    """kittrial-5bb.23 P2: a boolean short flag clustered with -C switched
    project because operator_only_flag stopped at the first unknown character.
    Per-command shorthand knowledge refuses the cluster instead.
    """

    def test_boolean_cluster_with_directory_refused(self):
        for args, expected in (
            (['list', '-rC/tmp/other'], '--directory'),
            (['list', '-wC/tmp/other'], '--directory'),
            (['ready', '-uC/tmp/other'], '--directory'),
            (['search', '-rCother', 'query'], '--directory'),
            (['list', '-rC'], '--directory'),
            (['show', '-wC/tmp/other'], '--directory'),
        ):
            with self.subTest(args=str(args)):
                self.assertEqual(operator_only_in_args(args), expected)
        # Without command context the conservative union also refuses it.
        self.assertIsNotNone(operator_only_flag('-rC'))

    def test_unknown_shorthand_fails_closed(self):
        self.assertIsNotNone(operator_only_in_args(['list', '-XC/tmp']))
        self.assertIsNotNone(operator_only_in_args(['state', '-X']))

    def test_plain_directory_spellings_still_refused(self):
        for token in ('-C/tmp', '-C=/tmp', '--directory=/tmp'):
            with self.subTest(token=token):
                self.assertIsNotNone(
                    operator_only_in_args(['list', token]))


class AssigneeShorthandTests(unittest.TestCase):
    """kittrial-5bb.23 P3: `-a` is --assignee on the list/ready/search/count/
    create/update commands and must keep working; it is --author only on
    `comments add`, where every spelling stays refused.
    """

    def test_assignee_shorthand_allowed_per_command(self):
        for command in ('list', 'ready', 'search', 'count', 'create', 'update'):
            for args in ([command, '-a', 'alice'],
                         [command, '-aalice'],
                         [command, '-a=alice']):
                with self.subTest(args=str(args)):
                    self.assertIsNone(operator_only_in_args(args))

    def test_author_shorthand_still_refused_on_comments_add(self):
        for args in (
            ['comments', 'add', 'task-1', 'hi', '-a', 'operator-x'],
            ['comments', 'add', 'task-1', 'hi', '-aoperator-x'],
            ['comments', 'add', 'task-1', 'hi', '-a=operator-x'],
            ['comments', 'add', 'task-1', 'hi', '--author=operator-x'],
            ['comments', 'add', 'task-1', 'hi', '--actor=operator-x'],
            ['comments', 'add', 'task-1', 'hi', '-qa', 'operator-x'],
        ):
            with self.subTest(args=str(args)):
                self.assertIsNotNone(operator_only_in_args(args))
        # `comments list` has no -a; the unknown shorthand fails closed too.
        self.assertIsNotNone(
            operator_only_in_args(['comments', 'list', '-a', 'alice']))

    def test_force_and_file_shorthands_are_per_command(self):
        # `close -f` is --force, not an operator file flag.
        self.assertIsNone(operator_only_in_args(['close', 'task-1', '-f']))
        self.assertIsNone(operator_only_in_args(['close', '-rf', 'done']))
        # -f stays a file flag on comments add and create.
        self.assertEqual(
            operator_only_in_args(['comments', 'add', 'task-1', 'x', '-f', 'notes']),
            '--file')
        self.assertEqual(
            operator_only_in_args(['create', 'title', '-f', 'notes.md']),
            '--file')


class ReservedLabelNamespaceTests(unittest.TestCase):
    """kittrial-5bb.23 P2 (addendum): contributors could plant request:/
    request-content: labels through create/update. Those namespaces are
    coordination-only; ordinary labels and read filters stay usable.
    """

    RESERVED = ['request:' + 'a' * 64, 'request-content:' + 'b' * 64]

    def test_reserved_request_labels_refused_on_writes(self):
        cases = (
            ['create', '--title', 'x', '--labels', self.RESERVED[0], '--json'],
            ['create', '--title', 'x', '--labels=' + self.RESERVED[1], '--json'],
            ['create', '--title', 'x', '-l', self.RESERVED[0]],
            ['create', '--title', 'x', '-l' + self.RESERVED[0]],
            ['create', '--title', 'x', '-ql', self.RESERVED[0]],
            ['create', '--title', 'x', '--labels', 'ok,' + self.RESERVED[0]],
            ['update', 'task-1', '--add-label', self.RESERVED[0]],
            ['update', 'task-1', '--add-label=' + self.RESERVED[1]],
            ['update', 'task-1', '--set-labels', self.RESERVED[0]],
            ['update', 'task-1', '--set-labels=' + self.RESERVED[1]],
            ['update', 'task-1', '--remove-label', self.RESERVED[0]],
        )
        for args in cases:
            with self.subTest(args=str(args)):
                self.assertIsNotNone(reserved_label_in_args(args))

    def test_ordinary_labels_and_read_filters_still_work(self):
        self.assertIsNone(reserved_label_in_args(
            ['create', '--title', 'x', '--labels', 'backend,reviewed']))
        self.assertIsNone(reserved_label_in_args(
            ['create', '--title', 'x', '-l', 'backend']))
        self.assertIsNone(reserved_label_in_args(
            ['create', '--title', 'x', '-ql', 'backend']))
        self.assertIsNone(reserved_label_in_args(
            ['update', 'task-1', '--add-label', 'reviewed']))
        self.assertIsNone(reserved_label_in_args(
            ['update', 'task-1', '--set-labels', 'a,b']))
        # Reads that filter on the reserved namespace are not label writes.
        self.assertIsNone(reserved_label_in_args(
            ['list', '-l', self.RESERVED[0]]))
        self.assertIsNone(reserved_label_in_args(
            ['ready', '--label', self.RESERVED[0]]))
        self.assertIsNone(reserved_label_in_args(None))

    def test_endpoint_order_refuses_before_native_write(self):
        # Mirror endpoint.execute's bd path order: operator-only check, then
        # reserved-label check, then raw-comment guard, then the single native
        # call. Neither a labelled create nor an attachment-before-add comment
        # may reach the native call.
        calls = []

        def native(args, attachments):
            calls.append((list(args), dict(attachments)))

        def guarded(args, attachments):
            if operator_only_in_args(args) is not None:
                raise ValueError('operator-only')
            if reserved_label_in_args(args) is not None:
                raise ValueError('reserved label')
            check_raw_request(args, attachments)
            native(args, attachments)

        with self.assertRaises(ValueError):
            guarded(['create', '--title', 'x', '--labels', self.RESERVED[0]], {})
        with self.assertRaises(ValueError):
            guarded(['comments', '@attachment:k', 'add', 'task-1'],
                    {'k': {'flag': '--file', 'text': forged_review_body()}})
        self.assertEqual(calls, [])
        guarded(['create', '--title', 'x', '--labels', 'backend'], {})
        self.assertEqual(len(calls), 1)


class DepSubcommandShorthandTests(unittest.TestCase):
    """kittrial-5bb.30 P3: `bd dep` keyed its whole shorthand inventory to the
    parent (`-b`), so the legitimate subcommand short forms `dep add -t`,
    `dep list -t` and `dep tree -d` were refused as operator-only. Native bd
    1.2.2 accepts them and rejects `-b` on those subcommands.
    """

    def test_dep_subcommand_short_forms_allowed(self):
        for args in (
            ['dep', 'add', 'task-2', 'task-1', '-t', 'related'],
            ['dep', 'add', 'task-2', 'task-1', '-t=related'],
            ['dep', 'add', 'task-2', 'task-1', '-trelated'],
            ['dep', 'list', 'task-1', '-t', 'blocks'],
            ['dep', 'tree', 'task-1', '-d', '3'],
            ['dep', 'tree', 'task-1', '-d3'],
        ):
            with self.subTest(args=str(args)):
                self.assertIsNone(operator_only_in_args(args), msg=str(args))

    def test_dep_parent_and_alias_forms_still_allowed(self):
        for args in (
            ['dep', 'task-1', '-b', 'task-2'],
            ['dep', 'task-1', '--blocks', 'task-2'],
            ['dep', 'rm', 'task-2', 'task-1'],
            ['dep', 'remove', 'task-2', 'task-1'],
            ['dep', 'relate', 'task-1', 'task-2'],
            ['dep', 'unrelate', 'task-1', 'task-2'],
            ['dep', 'cycles'],
            ['dep', 'add', 'task-2', 'task-1'],
            ['dep', '--json', 'list', 'task-1', '-t', 'blocks'],
        ):
            with self.subTest(args=str(args)):
                self.assertIsNone(operator_only_in_args(args), msg=str(args))

    def test_directory_smuggling_still_refused_for_dep(self):
        for args, expected in (
            (['dep', 'list', 'task-1', '-C/tmp/other'], '--directory'),
            (['dep', 'add', 'task-2', 'task-1', '-C', '/tmp/other'], '--directory'),
            (['dep', 'tree', 'task-1', '-hC/tmp'], '--directory'),
        ):
            with self.subTest(args=str(args)):
                self.assertEqual(operator_only_in_args(args), expected)
        # `-b` is unknown on `dep add` (bd rejects it natively), so that
        # subcommand's table fails closed on it instead of accepting it.
        self.assertIsNotNone(
            operator_only_in_args(['dep', 'add', 'task-2', 'task-1', '-b', 'task-1']))
        # A value-taking subcommand shorthand consumes the rest of the cluster,
        # so `-dC/tmp` is a depth value that bd itself rejects; it is not a
        # directory switch and the operator-only guard leaves it alone.
        self.assertIsNone(
            operator_only_in_args(['dep', 'tree', 'task-1', '-dC/tmp']))

    def test_unknown_dep_subcommand_shorthand_fails_closed(self):
        self.assertIsNotNone(
            operator_only_in_args(['dep', 'tree', 'task-1', '-XC/tmp']))
        self.assertIsNotNone(
            operator_only_in_args(['dep', 'add', 'a', 'b', '-qC/tmp']))


class CloseForceShorthandTests(unittest.TestCase):
    """kittrial-5bb.30 P3: `close -f` is --force, but endpoint's legacy
    FILE_FLAGS membership test refused the bare token as a raw server path.
    """

    def test_close_force_is_not_a_file_flag(self):
        self.assertIsNone(raw_file_flag_in_args(['close', 'task-1', '-f']))
        self.assertIsNone(raw_file_flag_in_args(['close', '-rf', 'done']))
        self.assertIsNone(operator_only_in_args(['close', 'task-1', '-f']))

    def test_file_flags_still_refused_where_bd_means_file(self):
        self.assertEqual(
            raw_file_flag_in_args(['create', 'title', '-f', 'notes.md']),
            '--file')
        self.assertEqual(
            raw_file_flag_in_args(['create', 'title', '-fnotes.md']),
            '--file')
        self.assertEqual(
            raw_file_flag_in_args(['comments', 'add', 'task-1', 'x', '-f', 'n']),
            '--file')
        for args, expected in (
            (['comments', 'add', 'task-1', 'x', '--file=n'], '--file'),
            (['update', 'task-1', '--body-file', 'n'], '--body-file'),
            (['update', 'task-1', '--design-file=n'], '--design-file'),
        ):
            with self.subTest(args=str(args)):
                self.assertEqual(raw_file_flag_in_args(args), expected)

    def test_double_dash_and_unknown_cluster_stay_closed(self):
        # `--` ends flag parsing: a later `-f` is an operand, not a flag.
        self.assertIsNone(raw_file_flag_in_args(['close', 'task-1', '--', '-f']))
        # An unknown shorthand is ambiguous and fails closed.
        self.assertIsNotNone(raw_file_flag_in_args(['state', '-Xf']))
        # `-rC/tmp` on list is a directory switch, caught by the operator-only
        # guard before the file-flag check (which is about raw paths only).
        self.assertIsNone(raw_file_flag_in_args(['list', '-rC/tmp']))
        self.assertEqual(operator_only_in_args(['list', '-rC/tmp']), '--directory')

    def test_endpoint_order_allows_force_close(self):
        # Mirror endpoint.execute's bd path: an operator-only flag or a raw
        # file path refuses before the native call; `close -f` reaches it once.
        calls = []

        def native(args):
            calls.append(list(args))

        def guarded(args):
            if operator_only_in_args(args) is not None:
                raise ValueError('operator-only')
            if raw_file_flag_in_args(args) is not None:
                raise ValueError('raw file path')
            native(args)

        guarded(['close', 'task-1', '-f'])
        self.assertEqual(calls, [['close', 'task-1', '-f']])
        with self.assertRaises(ValueError):
            guarded(['create', 'title', '-f', 'notes.md'])
        with self.assertRaises(ValueError):
            guarded(['show', 'task-1', '-C/tmp/other'])
        self.assertEqual(len(calls), 1)


class ReservedLabelMutationGuardTests(unittest.TestCase):
    """kittrial-5bb.30 P2: refusing a reserved label *value* left two routes
    open. bd copies parent labels onto `create --parent X` children unless
    --no-inherit-labels is given, and `update --set-labels` replaces the whole
    set, so a replacement naming no reserved value strips request:/
    request-content: from an operator-created holder. label_guard_request()
    describes the read-before-write check endpoint.execute applies under the
    project lock.
    """

    def test_create_parent_requires_a_parent_read(self):
        request = label_guard_request(
            ['create', 'inh', '--parent', 'task-1', '--json'])
        self.assertEqual(request['kind'], 'inherit')
        self.assertEqual(request['target'], 'task-1')
        self.assertFalse(request['ambiguous'])
        # --labels does not stop inheritance, so the guard still applies.
        request = label_guard_request(
            ['create', 'inh', '--labels', 'plain', '--parent', 'task-1'])
        self.assertEqual(request['target'], 'task-1')
        # Value flags before --parent are consumed, not mistaken for operands.
        request = label_guard_request(
            ['create', '-p', '1', '-l', 'a,b', '--parent=task-1', '--json'])
        self.assertEqual(request['target'], 'task-1')

    def test_no_inherit_labels_disables_the_guard(self):
        for args in (
            ['create', 'inh', '--parent', 'task-1', '--no-inherit-labels'],
            ['create', 'inh', '--no-inherit-labels', '--parent', 'task-1'],
            ['create', 'inh', '--parent', 'task-1', '--no-inherit-labels=true'],
        ):
            with self.subTest(args=str(args)):
                self.assertIsNone(label_guard_request(args))
        # An explicit false still inherits: keep guarding.
        self.assertIsNotNone(label_guard_request(
            ['create', 'inh', '--parent', 'task-1', '--no-inherit-labels=false']))

    def test_no_inherit_labels_parses_exactly_as_strconv_parsebool(self):
        # kittrial-5bb.30 review request bool-parse-mismatch: pflag uses Go's
        # strconv.ParseBool, so `=f`/`=F` mean False (bd inherits) and must
        # keep the guard active, while anything unparseable is rejected by bd
        # itself and must fail closed here too.
        for value in ('f', 'F', '0', 'false', 'False', 'FALSE'):
            with self.subTest(value=value):
                request = label_guard_request(
                    ['create', 'x', '--parent', 'H',
                     '--no-inherit-labels=' + value])
                self.assertIsNotNone(request)
                self.assertEqual(request['kind'], 'inherit')
                self.assertFalse(request['ambiguous'])
                self.assertEqual(request['target'], 'H')
        for value in ('1', 't', 'T', 'TRUE', 'true', 'True'):
            with self.subTest(value=value):
                self.assertIsNone(label_guard_request(
                    ['create', 'x', '--parent', 'H',
                     '--no-inherit-labels=' + value]))
        self.assertIsNone(label_guard_request(
            ['create', 'x', '--parent', 'H', '--no-inherit-labels']))
        for value in ('maybe', 'yes', '2', ''):
            with self.subTest(value=value):
                request = label_guard_request(
                    ['create', 'x', '--parent', 'H',
                     '--no-inherit-labels=' + value])
                self.assertIsNotNone(request)
                self.assertTrue(request['ambiguous'])
        # An unparseable value is refused even with no parent to read, so the
        # guard never lets a spelling bd rejects reach the native command.
        self.assertTrue(label_guard_request(
            ['create', 'x', '--no-inherit-labels=maybe'])['ambiguous'])
        # A repeated flag where any occurrence is unparseable fails closed,
        # because pflag errors on that occurrence regardless of order.
        self.assertTrue(label_guard_request(
            ['create', 'x', '--parent', 'H',
             '--no-inherit-labels=false',
             '--no-inherit-labels=maybe'])['ambiguous'])
        self.assertTrue(label_guard_request(
            ['create', 'x', '--parent', 'H',
             '--no-inherit-labels=maybe',
             '--no-inherit-labels=false'])['ambiguous'])
        self.assertIsNone(label_guard_request(
            ['create', 'x', '--parent', 'H',
             '--no-inherit-labels=false', '--no-inherit-labels=1']))

    def test_create_without_parent_needs_nothing(self):
        self.assertIsNone(label_guard_request(['create', 'x', '--json']))
        self.assertIsNone(label_guard_request(
            ['create', 'x', '--deps', 'parent-child:task-1', '--json']))
        self.assertIsNone(label_guard_request(['show', 'task-1']))
        self.assertIsNone(label_guard_request(None))

    def test_ambiguous_create_parent_fails_closed(self):
        # bd's last --parent wins, so a repeated flag must not be resolved by
        # taking the first occurrence.
        request = label_guard_request(
            ['create', 'x', '--parent', 'safe', '--parent', 'task-1'])
        self.assertTrue(request['ambiguous'])
        # A missing or empty parent value cannot be resolved.
        request = label_guard_request(['create', 'x', '--parent'])
        self.assertTrue(request['ambiguous'])
        request = label_guard_request(['create', 'x', '--parent='])
        self.assertTrue(request['ambiguous'])
        request = label_guard_request(
            ['create', 'x', '--mystery', '--parent', 'task-1'])
        self.assertTrue(request['ambiguous'])

    def test_decoy_parent_value_is_not_a_parent_flag(self):
        # `--title --parent X` makes --parent the title's value; the scan
        # consumes it, so this is not a create-with-parent at all.
        self.assertIsNone(label_guard_request(
            ['create', 'x', '--title', '--parent', 'task-1', '--json']))
        # A real --parent after the decoy is still found and guarded.
        request = label_guard_request(
            ['create', 'x', '--title', '--no-inherit-labels',
             '--parent', 'task-1'])
        self.assertEqual(request['kind'], 'inherit')
        self.assertFalse(request['ambiguous'])

    def test_update_label_replacement_requires_target_reads(self):
        request = label_guard_request(
            ['update', 'task-1', '--set-labels', 'plain'])
        self.assertEqual(request['kind'], 'replace')
        self.assertEqual(request['targets'], ['task-1'])
        self.assertFalse(request['ambiguous'])
        # Every named target is read (bd update accepts several ids).
        request = label_guard_request(
            ['update', 'task-1', 'task-2', '--remove-label', 'plain'])
        self.assertEqual(request['targets'], ['task-1', 'task-2'])
        # Label values are consumed, not read as targets.
        request = label_guard_request(
            ['update', 'task-1', '--set-labels', 'a,b', '--remove-label', 'c'])
        self.assertEqual(request['targets'], ['task-1'])
        # `@attachment:` transport placeholders are not ids.
        request = label_guard_request(
            ['update', 'task-1', '@attachment:k', '--set-labels', 'a'])
        self.assertEqual(request['targets'], ['task-1'])

    def test_update_without_replacing_flags_needs_nothing(self):
        self.assertIsNone(label_guard_request(
            ['update', 'task-1', '--add-label', 'plain']))
        self.assertIsNone(label_guard_request(
            ['update', 'task-1', '--parent', 'task-2']))
        self.assertIsNone(label_guard_request(
            ['update', 'task-1', '--status', 'in_progress', '--claim']))
        self.assertIsNone(label_guard_request(['create', 'x']))

    def test_no_target_or_unknown_flag_fails_closed(self):
        # bd falls back to the last touched issue; the guard cannot resolve it.
        self.assertTrue(label_guard_request(
            ['update', '--set-labels', 'plain'])['ambiguous'])
        self.assertTrue(label_guard_request(
            ['update', 'task-1', '--mystery', '--set-labels', 'a'])['ambiguous'])

    def test_first_reserved_label(self):
        self.assertEqual(first_reserved_label(['a', 'request:x']), 'request:x')
        self.assertEqual(
            first_reserved_label(['a', 'request-content:y']), 'request-content:y')
        self.assertIsNone(first_reserved_label(['a', 'b']))
        self.assertIsNone(first_reserved_label([]))
        self.assertIsNone(first_reserved_label(None))
        self.assertIsNone(first_reserved_label('request:x'))

    def test_guard_refuses_reserved_holder_before_native_write(self):
        # Mirror endpoint.execute's locked section: the read-before-write guard
        # runs inside the lock, before the single native mutation.
        calls = []
        holders = {'coordinated': ['request:REAL', 'plain'],
                   'ordinary': ['a', 'b']}

        def labels(task):
            return list(holders.get(task, []))

        def guarded(args):
            request = label_guard_request(args)
            if request is None:
                return native(args)
            if request['ambiguous']:
                raise ValueError('ambiguous label write')
            if request['kind'] == 'inherit':
                if first_reserved_label(labels(request['target'])) is not None:
                    raise ValueError('reserved parent')
                return native(args)
            for target in request['targets']:
                if first_reserved_label(labels(target)) is not None:
                    raise ValueError('reserved holder')
            return native(args)

        def native(args):
            calls.append(list(args))

        with self.assertRaises(ValueError):
            guarded(['create', 'inh', '--parent', 'coordinated', '--json'])
        with self.assertRaises(ValueError):
            guarded(['update', 'coordinated', '--set-labels', 'plain'])
        with self.assertRaises(ValueError):
            guarded(['update', 'coordinated', '--remove-label', 'plain'])
        self.assertEqual(calls, [])
        # Safe forms still reach the native write exactly once each.
        guarded(['create', 'inh', '--parent', 'coordinated',
                 '--no-inherit-labels', '--json'])
        guarded(['create', 'inh', '--parent', 'ordinary', '--json'])
        guarded(['update', 'ordinary', '--set-labels', 'a'])
        guarded(['update', 'coordinated', '--add-label', 'ok'])
        guarded(['close', 'coordinated', '-f'])
        self.assertEqual(len(calls), 5)


if __name__ == '__main__':
    unittest.main()
