"""Endpoint reserved-label guard: canonical id resolution and fail-closed reads.

Regression tests for kittrial-5bb.30 review request short-id-bypass. bd
resolves an issue id by unambiguous suffix (`bd show 3q2 --json` returns the row
`pp-3q2`), so endpoint._native_labels() must match the returned rows against the
argv token instead of requiring exact string equality, must name the resolved
canonical id in the refusal, and must raise when the token resolves to no row or
to several distinct rows. A read that cannot be resolved must never be read as
"this issue holds no reserved label".

The subprocess call to the real bd client is stubbed, so this module is
disposable and mutates nothing. endpoint.py imports fcntl, which is POSIX-only,
so the module is skipped where that import fails (Windows); Linux CI runs it.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    import endpoint
    from endpoint import _guard_record_anchor_status, _guard_reserved_labels, _native_labels
except ImportError:  # pragma: no cover - endpoint needs fcntl (POSIX)
    endpoint = None


class _Proc:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _rows_run(rows, returncode=0, stderr=''):
    """Stub endpoint.subprocess.run returning a bd-style JSON read."""
    def run(argv, **kwargs):
        return _Proc(returncode, json.dumps(rows), stderr)
    return run


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class NativeLabelResolutionTests(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='endpoint-guard-')
        self.root = Path(self._tmp.name)
        (self.root / 'bin').mkdir()
        (self.root / 'bin' / 'bd').write_text('', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            '{"password": "x", "unit": "none", "port": "1"}', encoding='utf-8')
        self.path = self.root / 'projects' / 'pp'
        self.path.mkdir(parents=True)
        self.addCleanup(self._tmp.cleanup)

    def labels(self, rows, token, **kwargs):
        with mock.patch.object(endpoint.subprocess, 'run',
                               _rows_run(rows, **kwargs)):
            return _native_labels(self.root, self.path, 'worker', token)

    def test_exact_id_returns_canonical_id_and_labels(self):
        rows = [{'id': 'pp-3q2', 'labels': ['request:B', 'plain']}]
        self.assertEqual(
            self.labels(rows, 'pp-3q2'),
            ('pp-3q2', {'request:B', 'plain'}))

    def test_bare_suffix_token_resolves_the_returned_row(self):
        # `bd show 3q2 --json` returns the row with the canonical id pp-3q2.
        rows = [{'id': 'pp-3q2', 'labels': ['request:B']}]
        self.assertEqual(self.labels(rows, '3q2'),
                         ('pp-3q2', {'request:B'}))

    def test_hierarchical_suffix_token_resolves_the_returned_row(self):
        rows = [{'id': 'pp-red.1', 'labels': ['request-content:x']}]
        self.assertEqual(self.labels(rows, 'red.1'),
                         ('pp-red.1', {'request-content:x'}))

    def test_single_dict_row_is_accepted(self):
        rows = {'id': 'pp-7bv', 'labels': ['request:x']}
        self.assertEqual(self.labels(rows, '7bv'), ('pp-7bv', {'request:x'}))

    def test_non_holder_row_returns_its_plain_labels(self):
        # Nothing reserved => the guard finds no reserved label and passes.
        rows = [{'id': 'pp-plain', 'labels': ['a', 'b']}]
        self.assertEqual(self.labels(rows, 'plain'),
                         ('pp-plain', {'a', 'b'}))
        rows = [{'id': 'pp-plain'}]
        self.assertEqual(self.labels(rows, 'plain'), ('pp-plain', set()))

    def test_unresolved_token_fails_closed(self):
        rows = [{'id': 'pp-other', 'labels': []}]
        with self.assertRaises(ValueError):
            self.labels(rows, '3q2')
        with self.assertRaises(ValueError):
            self.labels([], '3q2')
        with self.assertRaises(ValueError):
            self.labels([{'labels': ['request:x']}], '3q2')
        with self.assertRaises(ValueError):
            self.labels(['not-a-row'], '3q2')

    def test_several_distinct_matches_fail_closed(self):
        rows = [{'id': 'pp-3q2', 'labels': ['request:x']},
                {'id': 'ot-3q2', 'labels': []}]
        with self.assertRaises(ValueError):
            self.labels(rows, '3q2')

    def test_read_error_and_bad_output_fail_closed(self):
        with self.assertRaises(ValueError):
            self.labels([], 'pp-3q2', returncode=1, stderr='no such issue')
        with mock.patch.object(endpoint.subprocess, 'run',
                               lambda argv, **kw: _Proc(0, 'not json')):
            with self.assertRaises(ValueError):
                _native_labels(self.root, self.path, 'worker', 'pp-3q2')

    def test_guard_refuses_a_parent_holding_requirement_labels(self):
        # kittrial-pth.26 item 1: an accepted requirement parent must not hand
        # requirement/requirement:accepted to a raw `create --parent` child, so
        # a contributor cannot mint an accepted requirement by inheritance.
        rows = [{'id': 'pp-req.1',
                 'labels': ['requirement', 'requirement:accepted']}]
        with mock.patch.object(endpoint.subprocess, 'run', _rows_run(rows)):
            with self.assertRaises(ValueError) as caught:
                _guard_reserved_labels(self.root, self.path,
                                       ['create', 'child', '--parent', 'pp-req.1'],
                                       'mallory')
        message = str(caught.exception)
        self.assertIn('Refusing create --parent pp-req.1:', message)
        self.assertRegex(message, r'reserved label requirement')

    def test_guard_refuses_a_backfilled_requirement_parent_without_request_labels(self):
        # The .30 request-namespace guard cannot see this parent: it holds only
        # the controlled requirement labels (operator backfill). The extended
        # guard must still refuse the inheritance.
        rows = [{'id': 'pp-back.1', 'labels': ['requirement', 'requirement:draft']}]
        with mock.patch.object(endpoint.subprocess, 'run', _rows_run(rows)):
            with self.assertRaises(ValueError):
                _guard_reserved_labels(self.root, self.path,
                                       ['create', 'child', '--parent', 'pp-back.1'],
                                       'mallory')

    def test_guard_allows_explicit_no_inherit_from_a_requirement_parent(self):
        # The documented safe route: opt out of inheritance, then no reserved
        # label can reach the child and no native read is needed before it.
        for value in ('', '=1', '=true', '=T'):
            with self.subTest(value=value):
                with mock.patch.object(endpoint.subprocess, 'run') as run:
                    _guard_reserved_labels(
                        self.root, self.path,
                        ['create', 'child', '--parent', 'pp-req.1',
                         '--no-inherit-labels' + value], 'mallory')
                    run.assert_not_called()

    def test_guard_refuses_set_labels_on_a_requirement_holder(self):
        # kittrial-pth.26 item 2: `--set-labels` replaces the whole set, so a
        # replacement that names no reserved value would silently drop the
        # operator's acceptance and the coordination namespace.
        rows = [{'id': 'pp-req.1',
                 'labels': ['requirement', 'requirement:accepted']}]
        with mock.patch.object(endpoint.subprocess, 'run', _rows_run(rows)):
            with self.assertRaises(ValueError) as caught:
                _guard_reserved_labels(self.root, self.path,
                                       ['update', 'pp-req.1', '--set-labels',
                                        'keep'], 'mallory')
        message = str(caught.exception)
        self.assertIn('Refusing to replace labels on pp-req.1:', message)
        self.assertRegex(message, r'reserved label requirement')

    def test_guard_refuses_set_labels_on_a_mixed_namespace_holder(self):
        # A record holding both namespaces is refused too; the refusal names one
        # of the reserved labels it currently holds.
        rows = [{'id': 'pp-req.1',
                 'labels': ['requirement', 'requirement:accepted',
                            'request:' + 'a' * 64,
                            'request-content:' + 'b' * 64]}]
        with mock.patch.object(endpoint.subprocess, 'run', _rows_run(rows)):
            with self.assertRaises(ValueError) as caught:
                _guard_reserved_labels(self.root, self.path,
                                       ['update', 'pp-req.1', '--set-labels',
                                        'keep'], 'mallory')
        message = str(caught.exception)
        self.assertIn('Refusing to replace labels on pp-req.1:', message)
        self.assertRegex(message, r'reserved label (requirement|request)')

    def test_guard_refuses_remove_label_on_a_requirement_holder(self):
        rows = [{'id': 'pp-req.1', 'labels': ['requirement:accepted']}]
        with mock.patch.object(endpoint.subprocess, 'run', _rows_run(rows)):
            with self.assertRaises(ValueError):
                _guard_reserved_labels(self.root, self.path,
                                       ['update', 'pp-req.1', '--remove-label',
                                        'triage'], 'mallory')

    def test_guard_allows_ordinary_labels_on_a_requirement_holder(self):
        # --add-label can only add, and adding a reserved value is refused by
        # reserved_label_in_args(); ordinary labels stay available.
        rows = [{'id': 'pp-req.1', 'labels': ['requirement']}]
        with mock.patch.object(endpoint.subprocess, 'run', _rows_run(rows)):
            _guard_reserved_labels(self.root, self.path,
                                   ['update', 'pp-req.1', '--add-label',
                                    'reviewed'], 'mallory')
            _guard_reserved_labels(self.root, self.path,
                                   ['create', 'child', '--parent', 'pp-req.1',
                                    '--no-inherit-labels'], 'mallory')
        rows = [{'id': 'pp-plain', 'labels': ['a']}]
        with mock.patch.object(endpoint.subprocess, 'run', _rows_run(rows)):
            _guard_reserved_labels(self.root, self.path,
                                   ['update', 'pp-plain', '--set-labels', 'b'],
                                   'mallory')

    def test_guard_requirement_refusal_resolves_a_short_token(self):
        rows = [{'id': 'pp-req.1', 'labels': ['requirement:accepted']}]
        with mock.patch.object(endpoint.subprocess, 'run', _rows_run(rows)):
            with self.assertRaises(ValueError) as caught:
                _guard_reserved_labels(self.root, self.path,
                                       ['update', 'req.1', '--set-labels',
                                        'keep'], 'mallory')
        self.assertIn('Refusing to replace labels on pp-req.1:',
                      str(caught.exception))

    def test_guard_refuses_a_holder_reached_by_short_token(self):
        rows = [{'id': 'pp-3q2', 'labels': ['request:B', 'plain']}]
        with mock.patch.object(endpoint.subprocess, 'run', _rows_run(rows)):
            with self.assertRaises(ValueError) as caught:
                _guard_reserved_labels(self.root, self.path,
                                       ['update', '3q2', '--set-labels',
                                        'plain'], 'worker')
        # The refusal names the resolved canonical id, not the argv token.
        self.assertIn('Refusing to replace labels on pp-3q2:',
                      str(caught.exception))

    def test_guard_refuses_a_parent_reached_by_short_token(self):
        rows = [{'id': 'pp-3q2', 'labels': ['request:B']}]
        with mock.patch.object(endpoint.subprocess, 'run', _rows_run(rows)):
            with self.assertRaises(ValueError) as caught:
                _guard_reserved_labels(self.root, self.path,
                                       ['create', 'x', '--parent', '3q2'],
                                       'worker')
        self.assertIn('Refusing create --parent pp-3q2:',
                      str(caught.exception))

    def test_guard_allows_a_non_holder_short_token(self):
        rows = [{'id': 'pp-plain', 'labels': ['a']}]
        with mock.patch.object(endpoint.subprocess, 'run', _rows_run(rows)):
            _guard_reserved_labels(self.root, self.path,
                                   ['update', 'plain', '--set-labels', 'b'],
                                   'worker')
            _guard_reserved_labels(self.root, self.path,
                                   ['create', 'x', '--parent', 'plain'],
                                   'worker')

    def _status_run(self, labels, comments, calls=None):
        """Stub the ONE read `_guard_record_anchor_status` makes per call.

        `bd show ID... --json --include-comments` returns labels and comments
        together, so the whole guard is one native read however many ids the
        invocation names (kittrial-5bb.92 review item 1). One row is returned per
        named token, exactly as bd resolves each requested id.
        """
        def run(argv, **kwargs):
            if calls is not None:
                calls.append(list(argv))
            if 'show' not in argv:
                raise AssertionError(argv)
            tokens = [a for a in argv[argv.index('show') + 1:] if not a.startswith('--')]
            return _Proc(0, json.dumps([{'id': token, 'labels': labels, 'comments': comments}
                                        for token in tokens]))
        return run

    def test_status_guard_refuses_close_and_reopen_of_a_record_anchor(self):
        # kittrial-5bb.92 item 4: a record anchor is created closed on purpose and is
        # hidden from work; the raw endpoint must not reopen or close it.
        comments = [{'id': 'c-1', 'text': 'Kind: reference-entry-v1\n{"bad":1}', 'author': 'mallory',
                     'created_at': '2026-10-01T00:00:00Z'}]
        for argv in (['close', 'pp-3q2', '--reason', 'done'],
                     ['reopen', 'pp-3q2'],
                     ['update', 'pp-3q2', '--status', 'open']):
            with self.subTest(argv=argv), mock.patch.object(
                    endpoint.subprocess, 'run', self._status_run(['reference'], comments)):
                with self.assertRaisesRegex(ValueError, 'record anchor'):
                    _guard_record_anchor_status(self.root, self.path, argv, 'worker')

    def test_status_guard_refuses_every_status_and_assignee_spelling(self):
        # kittrial-5bb.92 review item 1: the short flag, --claim, --defer and
        # --assignee each returned 0 and moved the anchor before this.
        comments = [{'id': 'c-1', 'text': 'Kind: capability-entry-v1\n{"bad":1}', 'author': 'mallory',
                     'created_at': '2026-10-01T00:00:00Z'}]
        for argv in (['update', 'pp-3q2', '-s', 'open'],
                     ['update', 'pp-3q2', '-sopen'],
                     ['update', 'pp-3q2', '-s=open'],
                     ['update', '-s', 'open', 'pp-3q2'],
                     ['update', 'pp-3q2', 'task-9', '-s', 'in_progress'],
                     ['update', 'pp-3q2', '--claim'],
                     ['update', 'pp-3q2', '--defer', '+1d'],
                     ['update', 'pp-3q2', '--assignee', 'bob'],
                     ['update', 'pp-3q2', '-a', 'bob']):
            with self.subTest(argv=argv), mock.patch.object(
                    endpoint.subprocess, 'run', self._status_run(['capability'], comments)):
                with self.assertRaisesRegex(ValueError, 'record anchor'):
                    _guard_record_anchor_status(self.root, self.path, argv, 'worker')

    def test_status_guard_allows_a_requirement_record_anchor(self):
        # kittrial-5bb.92 review item 1 (coordinator correction): requirement/brd-section
        # records stay visible as work items, so the guard must NOT freeze them - they
        # behave exactly as on main. Only the reference/proposal/settings/capability
        # anchors stay guarded.
        comments = [{'id': 'c-1', 'text': 'Kind: requirement-revision-v1\n{"id": "pp-3q2"}',
                     'author': 'ops-james', 'created_at': '2026-10-01T00:00:00Z'}]
        for labels in (['requirement', 'requirement:draft'],
                       ['requirement', 'requirement:accepted'],
                       ['brd-section', 'requirement:accepted']):
            for argv in (['close', 'pp-3q2', '--reason', 'done'],
                         ['reopen', 'pp-3q2'],
                         ['update', 'pp-3q2', '-s', 'open'],
                         ['update', 'pp-3q2', '--claim'],
                         ['update', 'pp-3q2', '--assignee', 'lane-a'],
                         ['update', 'pp-3q2', '--defer', '+1d']):
                with self.subTest(labels=labels, argv=argv), mock.patch.object(
                        endpoint.subprocess, 'run', self._status_run(labels, comments)):
                    self.assertIsNone(
                        _guard_record_anchor_status(self.root, self.path, argv, 'worker'))

    def test_an_accepted_requirement_record_is_claimed_and_closed_through_the_endpoint(self):
        # kittrial-5bb.92 review item 1: on jjbp six requirement records are in_progress
        # and assigned to worker lanes (three accepted). A contributor must still be able
        # to claim, close, reopen, reassign and defer them through the endpoint, so each
        # one reaches the native write instead of being refused by the status guard.
        (self.path / '.beads').mkdir()
        (self.path / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        rows = [{'id': 'pp-req.1', 'status': 'in_progress', 'assignee': 'lane-a',
                 'labels': ['requirement', 'requirement:accepted'],
                 'comments': [{'id': 'c-1', 'author': 'ops-james',
                               'created_at': '2026-10-01T00:00:00Z',
                               'text': 'Kind: requirement-revision-v1\n{"id": "pp-req.1"}'},
                              {'id': 'c-2', 'author': 'ops-james',
                               'created_at': '2026-10-02T00:00:00Z',
                               'text': 'Kind: requirement-acceptance-v1\n{"id": "pp-req.1"}'}]}]
        writes = []

        def run(argv, **kwargs):
            if 'show' in argv:
                tokens = [token for token in argv[argv.index('show') + 1:]
                          if not token.startswith('--')]
                return _Proc(0, json.dumps([row for row in rows if row['id'] in tokens]))
            writes.append(list(argv))
            return _Proc(0, json.dumps({'ok': True}))

        for args in (['update', 'pp-req.1', '--claim'],
                     ['close', 'pp-req.1', '--reason', 'done'],
                     ['reopen', 'pp-req.1'],
                     ['update', 'pp-req.1', '--assignee', 'lane-b'],
                     ['update', 'pp-req.1', '--defer', '+1d']):
            with self.subTest(args=args), mock.patch.object(endpoint.subprocess, 'run', run):
                answer = endpoint.execute(self.root, {'project': 'pp', 'actor': 'worker',
                                                      'action': 'bd', 'args': args})
                self.assertEqual(answer['returncode'], 0, answer.get('stderr'))
        self.assertEqual(len(writes), 5)

    def test_status_guard_reads_every_target_in_one_native_read(self):
        # kittrial-5bb.92 review item 1: keep the read-before-write to one read per
        # call, whatever the number of named ids.
        calls = []
        comments = [{'id': 'c-1', 'text': 'an ordinary note', 'author': 'worker'}]
        argv = ['close', 'pp-3q2', 'pp-4a1', 'pp-5b2', 'pp-6c3', 'pp-7d4', '--reason', 'done']
        with mock.patch.object(endpoint.subprocess, 'run',
                               self._status_run(['plain'], comments, calls=calls)):
            _guard_record_anchor_status(self.root, self.path, argv, 'worker')
        self.assertEqual(len(calls), 1)
        self.assertIn('--include-comments', calls[0])

    def test_status_guard_allows_an_ordinary_task(self):
        comments = [{'id': 'c-1', 'text': 'an ordinary note', 'author': 'worker'}]
        for argv in (['close', 'pp-3q2', '--reason', 'done'],
                     ['reopen', 'pp-3q2'],
                     ['update', 'pp-3q2', '-s', 'open'],
                     ['update', 'pp-3q2', '--claim'],
                     ['update', 'pp-3q2', '--defer', '+1d'],
                     ['update', 'pp-3q2', '--assignee', 'bob']):
            with self.subTest(argv=argv), mock.patch.object(
                    endpoint.subprocess, 'run', self._status_run(['plain'], comments)):
                _guard_record_anchor_status(self.root, self.path, argv, 'worker')

    def test_status_guard_makes_no_read_when_no_status_or_assignee_moves(self):
        # The flag scan itself is free: an ordinary title/description/label write
        # must not pay for a status guard read.
        with mock.patch.object(endpoint.subprocess, 'run') as run:
            for argv in (['update', 'pp-3q2', '--title', 'x'],
                         ['update', 'pp-3q2', '--description', 'text'],
                         ['update', 'pp-3q2', '--add-label', 'bug'],
                         ['update', 'pp-3q2', '--claim=false']):
                with self.subTest(argv=argv):
                    _guard_record_anchor_status(self.root, self.path, argv, 'worker')
            run.assert_not_called()

    def test_status_guard_fails_closed_without_a_named_target(self):
        for argv in (['close', '--json'], ['close', 'pp-3q2', '--mystery'],
                     ['update', '-s', 'open'], ['update', '--claim'],
                     ['update', '--defer', '+1d'], ['update', '--assignee', 'bob']):
            with self.subTest(argv=argv), mock.patch.object(endpoint.subprocess, 'run', _rows_run([])):
                with self.assertRaises(ValueError):
                    _guard_record_anchor_status(self.root, self.path, argv, 'worker')

    def test_guard_refuses_unresolved_token_before_any_read_use(self):
        rows = [{'id': 'pp-other', 'labels': []}]
        with mock.patch.object(endpoint.subprocess, 'run', _rows_run(rows)):
            with self.assertRaises(ValueError):
                _guard_reserved_labels(self.root, self.path,
                                       ['update', '3q2', '--set-labels',
                                        'plain'], 'worker')

    def test_guard_refuses_ambiguous_bool_without_a_native_read(self):
        # An unparseable --no-inherit-labels value fails closed before bd runs,
        # so no native write (and no read) is attempted for it.
        with mock.patch.object(endpoint.subprocess, 'run') as run:
            with self.assertRaises(ValueError):
                _guard_reserved_labels(
                    self.root, self.path,
                    ['create', 'x', '--parent', 'H',
                     '--no-inherit-labels=maybe'], 'worker')
            run.assert_not_called()

    def test_guard_keeps_guarding_on_a_false_go_literal(self):
        rows = [{'id': 'pp-3q2', 'labels': ['request:B']}]
        for value in ('f', 'F', '0', 'false', 'False', 'FALSE'):
            with self.subTest(value=value):
                with mock.patch.object(endpoint.subprocess, 'run',
                                       _rows_run(rows)):
                    with self.assertRaises(ValueError):
                        _guard_reserved_labels(
                            self.root, self.path,
                            ['create', 'x', '--parent', '3q2',
                             '--no-inherit-labels=' + value], 'worker')

    def test_guard_disabled_by_a_true_go_literal(self):
        for value in ('1', 't', 'T', 'TRUE', 'true', 'True'):
            with self.subTest(value=value):
                with mock.patch.object(endpoint.subprocess, 'run') as run:
                    _guard_reserved_labels(
                        self.root, self.path,
                        ['create', 'x', '--parent', '3q2',
                         '--no-inherit-labels=' + value], 'worker')
                    run.assert_not_called()

    def test_repeated_flag_last_occurrence_decides_the_guard(self):
        # kittrial-5bb.30 review request repeated-flag-last-wins: bd's pflag
        # takes the LAST --no-inherit-labels occurrence, so a trailing false
        # still inherits and must be refused before any native write.
        rows = [{'id': 'pp-3q2', 'labels': ['request:B']}]
        for args in (
            ['create', 'x', '--parent', '3q2',
             '--no-inherit-labels', '--no-inherit-labels=false'],
            ['create', 'x', '--parent', '3q2',
             '--no-inherit-labels=true', '--no-inherit-labels=F'],
            ['create', 'x', '--parent', '3q2',
             '--no-inherit-labels=1', '--no-inherit-labels=f'],
        ):
            with self.subTest(args=str(args)):
                with mock.patch.object(endpoint.subprocess, 'run',
                                       _rows_run(rows)):
                    with self.assertRaises(ValueError):
                        _guard_reserved_labels(self.root, self.path, args,
                                               'worker')

    def test_repeated_flag_true_last_disables_the_guard_without_a_read(self):
        for args in (
            ['create', 'x', '--parent', '3q2',
             '--no-inherit-labels=false', '--no-inherit-labels=true'],
            ['create', 'x', '--parent', '3q2',
             '--no-inherit-labels=F', '--no-inherit-labels'],
        ):
            with self.subTest(args=str(args)):
                with mock.patch.object(endpoint.subprocess, 'run') as run:
                    _guard_reserved_labels(self.root, self.path, args, 'worker')
                    run.assert_not_called()

    def test_repeated_flag_with_an_invalid_occurrence_fails_closed(self):
        # pflag rejects the command on the first unparseable occurrence, so the
        # endpoint must refuse before running bd at all.
        for args in (
            ['create', 'x', '--parent', '3q2',
             '--no-inherit-labels=false', '--no-inherit-labels=maybe'],
            ['create', 'x', '--parent', '3q2',
             '--no-inherit-labels=maybe', '--no-inherit-labels=false'],
            ['create', 'x', '--parent', '3q2',
             '--no-inherit-labels', '--no-inherit-labels=yes'],
            ['create', 'x', '--parent', '3q2',
             '--no-inherit-labels=2', '--no-inherit-labels'],
        ):
            with self.subTest(args=str(args)):
                with mock.patch.object(endpoint.subprocess, 'run') as run:
                    with self.assertRaises(ValueError):
                        _guard_reserved_labels(self.root, self.path, args,
                                               'worker')
                    run.assert_not_called()


class _EndpointRootMixin:
    """A disposable root/project for driving endpoint.execute end to end."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='endpoint-dispatch-')
        self.root = Path(self._tmp.name)
        (self.root / 'bin').mkdir()
        (self.root / 'bin' / 'bd').write_text('', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            '{"password": "x", "unit": "none", "port": "1"}', encoding='utf-8')
        (self.root / 'projects' / 'pp' / '.beads').mkdir(parents=True)
        (self.root / 'projects' / 'pp' / '.beads' / 'metadata.json').write_text(
            '{}', encoding='utf-8')
        self.addCleanup(self._tmp.cleanup)

    def execute(self, action, payload, actor='alice'):
        return endpoint.execute(self.root, {
            'project': 'pp', 'actor': actor, 'action': action,
            'args': [json.dumps(payload)]})

    def bd(self, args, actor='mallory'):
        return endpoint.execute(self.root, {
            'project': 'pp', 'actor': actor, 'action': 'bd', 'args': args})

    def out(self, result):
        return json.loads(result['stdout'])


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointSingleDispatchTests(_EndpointRootMixin, unittest.TestCase):
    """kittrial-pth.26 rev3 P1 endpoint-double-dispatch.

    endpoint.execute kept the lifecycle/coordinate block twice, so both actions
    ran twice under the lock and the caller saw the second, reconciled result: a
    fresh merge-release returned {released:false,available:true} and a fresh
    lifecycle write returned reconciled:true. Each request must dispatch once.
    """

    def test_lifecycle_dispatches_exactly_once(self):
        calls = []
        passed = {}

        def apply(payload, actor, run, **kwargs):
            calls.append(payload)
            passed.update(kwargs)
            return {'event_id': 'e1', 'reconciled': len(calls) > 1}

        with mock.patch.object(endpoint, 'apply_native', apply):
            result = self.execute('lifecycle',
                                  {'schema_version': 1, 'operation_id': 'lc1'})
        self.assertEqual(len(calls), 1)
        self.assertFalse(self.out(result)['reconciled'])
        # The endpoint resolves the operator allowlist and the project journal for
        # the release revert rule (kittrial-5bb.107 item 2) and passes both down.
        self.assertEqual(set(passed), {'operators', 'journal'})
        self.assertEqual(passed['journal'], self.root / 'projects' / 'pp')

    def test_coordinate_dispatches_exactly_once(self):
        import coordination
        counts = {}

        def apply(payload, actor, run, path):
            operation = payload.get('operation')
            counts[operation] = counts.get(operation, 0) + 1
            if operation == 'merge-release':
                return {'released': counts[operation] == 1}
            return {'reconciled': counts[operation] > 1}

        with mock.patch.object(coordination, 'apply_native', apply):
            release = self.out(self.execute('coordinate',
                                            {'operation': 'merge-release'}))
            acquire = self.out(self.execute('coordinate',
                                            {'operation': 'merge-acquire'}))
        self.assertEqual(counts, {'merge-release': 1, 'merge-acquire': 1})
        self.assertTrue(release['released'])
        self.assertFalse(acquire['reconciled'])

    def test_requirement_dispatches_exactly_once_not_lifecycle(self):
        import requirement_records
        calls = []

        def apply(payload, actor, run, path):
            calls.append(payload)
            return {'id': 'r1', 'reconciled': len(calls) > 1}

        with mock.patch.object(requirement_records, 'apply_native', apply):
            result = self.execute('requirement', {'operation_id': 'r1'})
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.out(result)['id'], 'r1')


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointLabelAliasGuardTests(_EndpointRootMixin, unittest.TestCase):
    """kittrial-pth.26 rev3 P1 hidden-label-alias at the endpoint seam."""

    RESERVED = ('requirement', 'requirement:accepted', 'requirement:draft',
                'brd-section', 'request:' + 'a' * 64,
                'requirement,requirement:accepted')

    def test_create_label_alias_is_refused_before_any_native_write(self):
        with mock.patch.object(endpoint.subprocess, 'run') as run:
            for value in self.RESERVED:
                for args in (['create', 'x', '--label', value],
                             ['create', 'x', '--label=' + value]):
                    with self.subTest(args=str(args)):
                        with self.assertRaisesRegex(
                                ValueError, 'Reserved coordination/requirement labels'):
                            self.bd(args)
            run.assert_not_called()

    def test_unrecognized_create_update_flag_is_refused(self):
        with mock.patch.object(endpoint.subprocess, 'run') as run:
            for args in (['create', 'x', '--labell', 'requirement'],
                         ['update', 'x', '--set-label', 'requirement'],
                         ['update', 'x', '--mystery']):
                with self.subTest(args=str(args)):
                    with self.assertRaisesRegex(ValueError, 'unrecognized flag'):
                        self.bd(args)
            run.assert_not_called()

    def test_ordinary_labels_still_reach_the_native_client_once(self):
        def proc(argv, **kwargs):
            return _Proc(0, '{"id": "pp-1"}', '')

        for args in (['create', 'x', '--label', 'frontend,bug', '--json'],
                     ['create', 'x', '--labels', 'frontend', '--json'],
                     ['create', 'x', '-l', 'frontend', '--json'],
                     ['update', 'pp-1', '--add-label', 'reviewed', '--json']):
            with self.subTest(args=str(args)):
                with mock.patch.object(endpoint.subprocess, 'run', proc):
                    result = self.bd(args)
                self.assertEqual(result['returncode'], 0)


if __name__ == '__main__':
    unittest.main()
