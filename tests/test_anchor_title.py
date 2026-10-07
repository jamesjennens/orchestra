"""The title of a record anchor is not changed through bd (kittrial-5bb.97, the coordinator's addition).

The kit finds a reference, a proposal, the proposal settings and a capability by the title
of its anchor row. `update --title` on such a row, through the endpoint's bd path, is refused
before the write; requirement and brd-section records, which are worked as tasks, are renamed
like tasks.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import reserved_comments

try:
    import endpoint
except ImportError:  # endpoint needs fcntl (POSIX)
    endpoint = None


class ScanTests(unittest.TestCase):
    def test_which_invocations_change_a_title_and_which_rows_they_name(self):
        targets = reserved_comments.title_change_targets
        for argv, expected in (
                (['update', 'pp-1', '--title', 'x'], ['pp-1']),
                (['update', '--title', 'x', 'pp-1'], ['pp-1']),
                (['update', '--title=x', 'pp-1', 'pp-2'], ['pp-1', 'pp-2']),
                (['update', 'pp-1', '--status', 'open', '--title', 'x'], ['pp-1']),
                (['update', 'pp-1', '--title', ''], ['pp-1']),
                # Names no row, or cannot be scanned: bd would act on the row it touched last.
                (['update', '--title', 'x'], 'unnamed'),
                (['update', 'pp-1', '--title', 'x', '--no-such-flag'], 'unnamed'),
                # Not a title change.
                (['update', 'pp-1', '--status', 'open'], None),
                (['update', 'pp-1', '--description', '--title'], None),         # the value of another flag
                (['update', 'pp-1', '--', '--title', 'x'], None),               # after the end of the flags
                (['create', '--title', 'x'], None),
                (['close', 'pp-1'], None),
                ([], None), ('update --title x', None)):
            with self.subTest(argv=argv):
                self.assertEqual(targets(argv), expected)

    def test_bd_update_has_no_short_flag_for_the_title(self):
        """The guard looks for --title alone; this pins that the kit's own flag table knows no other spelling."""
        self.assertIn('--title', reserved_comments.BD_LONG_VALUE_FLAGS['update'])
        table = reserved_comments._short_flag_table('update')
        flags, operands, unknown = reserved_comments._bd_scan(['update', 'pp-1', '-t', 'x'], 'update')
        self.assertTrue('t' not in table or unknown or ('-t', 'x') not in [(n, v) for n, v in flags if n == '--title'])
        # Whatever -t is to bd update, it is not scanned as the title; were bd to add one, this fails.
        self.assertNotIn('--title', [name for name, _ in flags])


class _Proc:
    def __init__(self, code, out='', err=''):
        self.returncode, self.stdout, self.stderr = code, out, err


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class GuardTests(unittest.TestCase):
    ANCHORS = (('reference', 'Kind: reference-entry-v1\n{"bad":1}'),
               ('capability', 'Kind: capability-entry-v1\n{"bad":1}'),
               ('proposal', 'Kind: proposal-entry-v1\n{"bad":1}'))

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / 'projects' / 'pp'
        (self.path / '.beads').mkdir(parents=True)
        (self.root / 'deployment.private.json').write_text('{"password": "x"}', encoding='utf-8')

    def reads(self, rows_by_id, calls):
        def run(argv, **kwargs):
            calls.append(list(argv))
            if 'show' not in argv:
                raise AssertionError(argv)
            tokens = [a for a in argv[argv.index('show') + 1:] if not a.startswith('--')]
            return _Proc(0, json.dumps([dict({'id': token}, **rows_by_id.get(token, {'labels': [], 'comments': []}))
                                        for token in tokens]))
        return run

    def anchor(self, label, text):
        return {'labels': [label], 'comments': [{'id': 'c-1', 'text': text, 'author': 'mallory',
                                                 'created_at': '2026-10-01T00:00:00Z'}]}

    def guard(self, argv, rows):
        calls = []
        with mock.patch.object(endpoint.subprocess, 'run', self.reads(rows, calls)):
            endpoint._guard_record_anchor_title(self.root, self.path, argv, 'worker')
        return calls

    def test_a_title_change_on_an_anchor_is_refused_in_every_spelling(self):
        for label, text in self.ANCHORS:
            row = self.anchor(label, text)
            if not reserved_comments.is_record_anchor(dict(row, id='pp-3q2')):
                continue                                          # a family this kit does not read as an anchor
            for argv in (['update', 'pp-3q2', '--title', 'renamed'],
                         ['update', '--title=renamed', 'pp-3q2'],
                         ['update', 'task-9', 'pp-3q2', '--title', 'renamed'],
                         ['update', 'pp-3q2', '--priority', '1', '--title', 'renamed']):
                with self.subTest(label=label, argv=argv):
                    with self.assertRaises(ValueError) as refused:
                        self.guard(argv, {'pp-3q2': row})
                    self.assertEqual(str(refused.exception),
                                     'Refusing to update the title of pp-3q2: it is a reference/proposal/settings/capability '
                                     'record anchor, and its title is how the kit finds it.')

    def test_at_least_the_reference_and_capability_anchors_are_covered(self):
        covered = [label for label, text in self.ANCHORS
                   if reserved_comments.is_record_anchor(dict(self.anchor(label, text), id='pp-1'))]
        self.assertIn('reference', covered)
        self.assertIn('capability', covered)

    def test_an_ordinary_row_and_a_requirement_record_are_renamed_as_before(self):
        for row in ({'labels': [], 'comments': []},
                    {'labels': ['reference'], 'comments': []},                      # the label alone is not an anchor
                    {'labels': ['requirement'], 'comments': [{'id': 'c-1', 'text': 'Kind: requirement-v1\n{}',
                                                              'author': 'a', 'created_at': '2026-10-01T00:00:00Z'}]}):
            with self.subTest(row=row):
                calls = self.guard(['update', 'pp-7', '--title', 'renamed'], {'pp-7': row})
                self.assertEqual(len(calls), 1)                   # one native read, whatever it names

    def test_all_named_rows_are_read_in_one_read_and_nothing_is_read_for_another_write(self):
        calls = self.guard(['update', 'pp-1', 'pp-2', 'pp-3', '--title', 'x'], {})
        self.assertEqual(len(calls), 1)
        for argv in (['update', 'pp-1', '--status', 'open'], ['update', 'pp-1', '--description', 'x'],
                     ['create', '--title', 'x'], ['close', 'pp-1']):
            with self.subTest(argv=argv):
                self.assertEqual(self.guard(argv, {}), [])

    def test_with_the_rows_the_write_already_read_only_a_labelled_row_is_read_again(self):
        """Every write reads its rows once (kittrial-5bb.113); a rename of ordinary rows must cost no second read."""
        def guard(named, rows):
            calls = []
            with mock.patch.object(endpoint.subprocess, 'run', self.reads(rows, calls)):
                endpoint._guard_record_anchor_title(self.root, self.path, ['update', *[r['id'] for r in named], '--title', 'x'],
                                                    'worker', named)
            return calls
        plain = [{'id': 'pp-1', 'labels': []}, {'id': 'pp-2', 'labels': ['bug']}, {'id': 'pp-3'}]
        self.assertEqual(guard(plain, {}), [])
        # A row with a record label is read again, alone, with its comments: the label is not the proof.
        labelled = plain + [{'id': 'pp-9', 'labels': ['reference']}]
        calls = guard(labelled, {'pp-9': {'labels': ['reference'], 'comments': []}})
        self.assertEqual(len(calls), 1)
        self.assertIn('pp-9', calls[0])
        self.assertIn('--include-comments', calls[0])
        self.assertNotIn('pp-1', calls[0])
        # And when it is an anchor, the write is refused.
        with self.assertRaisesRegex(ValueError, 'Refusing to update the title of pp-9: it is a reference'):
            guard(labelled, {'pp-9': self.anchor(*self.ANCHORS[0])})

    def test_the_bd_action_hands_the_guard_the_rows_it_read(self):
        source = (KIT / 'endpoint.py').read_text(encoding='utf-8')
        self.assertIn("named=_guard_named_rows(root,path,name,args,request.get('attachments',{}),actor)", source)
        self.assertIn('_guard_record_anchor_title(root,path,args,actor,named)', source)

    def test_a_title_change_that_names_no_row_is_refused(self):
        for argv in (['update', '--title', 'x'], ['update', 'pp-1', '--title', 'x', '--no-such-flag']):
            with self.subTest(argv=argv), self.assertRaisesRegex(ValueError, 'name exactly the issue'):
                self.guard(argv, {})

    def test_the_guard_runs_before_the_write_of_the_bd_action(self):
        source = (KIT / 'endpoint.py').read_text(encoding='utf-8')
        block = source[source.index('            named=_guard_named_rows(root,path,name,args'):source.index('            def bd_dispatch(argv):')]
        self.assertIn('_guard_record_anchor_title(root,path,args,actor,named)', block)
        self.assertLess(block.index('_guard_record_anchor_status'), block.index('_guard_record_anchor_title'))


if __name__ == '__main__':
    unittest.main()
