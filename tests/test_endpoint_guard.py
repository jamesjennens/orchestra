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
    from endpoint import _guard_reserved_labels, _native_labels
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


if __name__ == '__main__':
    unittest.main()
