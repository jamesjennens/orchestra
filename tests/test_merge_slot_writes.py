"""Every contributor write names the rows it writes; none may be the merge slot; a damaged slot is repaired.

Reviews 01a109cc and 01a10c0b of kittrial-5bb.113. Refusing only status and assignee
moves left the slot writable; guessing which tokens could reach it then missed the
writes that name no row at all (no id, ``close --claim-next``, ``create --id``, a file of
dependency edges) and the lone dash, which bd resolves to the slot. Nothing is guessed
now: each command's row-naming tokens are listed and resolved through bd.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import coordination
import reserved_comments as rc

try:
    import endpoint
except ImportError:  # endpoint needs fcntl (POSIX)
    endpoint = None

SLOT = 'pp-merge-slot'
OTHER = 'pp-abc'
THIRD = 'pp-def'
SENTENCE = SLOT + " is the project's merge slot, an internal record, not a task"


def slot_row(**changes):
    row = {'id': SLOT, 'title': 'Merge Slot', 'issue_type': 'task', 'status': 'open', 'assignee': None,
           'labels': ['gt:slot'], 'metadata': None, 'comments': []}
    row.update(changes)
    return row


def edges(*pairs):
    return {'0': {'flag': '--file', 'text': '\n'.join(json.dumps(pair) for pair in pairs)}}


class SlotIdTests(unittest.TestCase):
    """The slot is the exact id PROJECT-merge-slot; a project name holds no hyphen."""

    def test_the_exact_shape_and_nothing_near_it(self):
        for value in ('pp-merge-slot', 'alpha-merge-slot', 'a1-merge-slot', 'kittrial-merge-slot'):
            self.assertTrue(rc.is_merge_slot_id(value), value)
        for value in ('pp-x-merge-slot', 'pp-merge-slot.1', 'merge-slot', '-merge-slot', 'PP-merge-slot', 'pp-merge-slots',
                      'p-merge-slot', 'pp-merge-slot ', 'pp-merge-slot\n', 'x' * 25 + '-merge-slot', '9p-merge-slot', None, 7):
            self.assertFalse(rc.is_merge_slot_id(value), value)

    def test_the_row_rule_is_the_id_alone(self):
        self.assertTrue(coordination.is_merge_slot(slot_row()))
        # The label is what an accident removes: without it the row is still the slot.
        self.assertTrue(coordination.is_merge_slot(slot_row(labels=[])))
        self.assertTrue(coordination.is_merge_slot(slot_row(labels=None, status='in_progress', assignee='eve')))
        # A row that merely carries the label, or whose id merely ends that way, is a task.
        self.assertFalse(coordination.is_merge_slot(slot_row(id='pp-abc')))
        self.assertFalse(coordination.is_merge_slot(slot_row(id='pp-x-merge-slot')))
        self.assertFalse(coordination.is_merge_slot(slot_row(id='pp-merge-slot.1')))
        self.assertEqual(rc.MERGE_SLOT_SUFFIX, coordination.MERGE_SLOT_SUFFIX)
        self.assertEqual(rc.MERGE_SLOT_LABEL, coordination.MERGE_SLOT_LABEL)


class TargetTests(unittest.TestCase):
    """Which tokens of each command name rows, and which forms name none."""

    def targets(self, argv, attachments=None):
        found = rc.write_targets(argv, attachments)
        self.assertIsNotNone(found, argv)
        self.assertIsNone(found['refusal'], (argv, found['refusal']))
        return found['targets']

    def refusal(self, argv, attachments=None):
        found = rc.write_targets(argv, attachments)
        self.assertIsNotNone(found, argv)
        self.assertTrue(found['refusal'], argv)
        return found['refusal']

    def test_the_rows_each_writing_command_names(self):
        cases = [
            (['update', OTHER, '--title', 'slot'], [OTHER]),                 # a title is not an id
            (['update', OTHER, THIRD, '--priority', '1'], [OTHER, THIRD]),
            (['update', OTHER, '--parent', THIRD], [OTHER, THIRD]),
            (['update', OTHER, '--parent=' + THIRD], [OTHER, THIRD]),
            (['update', OTHER, '--parent', ''], [OTHER]),                    # removing a parent names nothing
            (['update', '-', '--title', 'x'], ['-']),                        # bd resolves the lone dash
            (['close', OTHER, '--reason', 'slot'], [OTHER]),
            (['close', OTHER, '--suggest-next'], [OTHER]),
            (['close', OTHER, '--claim-next=false'], [OTHER]),
            (['reopen', OTHER, THIRD], [OTHER, THIRD]),
            (['create', 'slot', '--description', 'merge-slot'], []),          # the title of a new task
            (['create', 'x', '--parent', OTHER], [OTHER]),
            (['create', 'x', '--deps', 'blocks:' + OTHER + ',' + THIRD], [OTHER, THIRD]),
            (['create', 'x', '--deps', 'external:proj:cap'], []),
            (['create', 'x', '--waits-for', OTHER], [OTHER]),
            (['comments', 'add', OTHER, 'a note'], [OTHER]),
            (['comments', 'add', '-', 'a note'], ['-']),
            (['dep', 'add', OTHER, THIRD], [OTHER, THIRD]),
            (['dep', 'add', OTHER, '-'], [OTHER, '-']),
            (['dep', 'add', OTHER, THIRD, '-t', 'blocks'], [OTHER, THIRD]),
            (['dep', 'add', OTHER, THIRD, '--type=related', '--no-cycle-check'], [OTHER, THIRD]),
            (['dep', 'add', OTHER, 'external:proj:cap'], [OTHER]),
            (['dep', 'remove', OTHER, THIRD], [OTHER, THIRD]),
            (['dep', 'rm', OTHER, THIRD], [OTHER, THIRD]),
            (['dep', 'relate', OTHER, THIRD], [OTHER, THIRD]),
            (['dep', 'unrelate', OTHER, THIRD], [OTHER, THIRD]),
            (['dep', OTHER, '--blocks', THIRD], [OTHER, THIRD]),
            (['dep', OTHER, '--blocks=' + THIRD], [OTHER, THIRD]),
            (['dep', OTHER, '-b', THIRD], [OTHER, THIRD]),
            (['dep', OTHER, '-b' + THIRD], [OTHER, THIRD]),
            (['dep', 'add', OTHER, '--blocked-by', THIRD], [OTHER, THIRD]),
            (['dep', 'add', OTHER, '--depends-on', THIRD], [OTHER, THIRD]),
            (['dep', 'add', OTHER, OTHER], [OTHER]),                         # named once
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(self.targets(argv), expected)

    def test_reads_are_not_writes(self):
        for argv in (['show', SLOT], ['list', '--label', 'gt:slot'], ['ready'], ['search', 'slot'], ['count'],
                     ['state', SLOT, 'mode'], ['lint', SLOT], ['comments', SLOT], ['dep', 'list', SLOT],
                     ['dep', 'tree', SLOT], ['dep', 'cycles'], ['dep'], [], 'update x'):
            with self.subTest(argv=argv):
                self.assertIsNone(rc.write_targets(argv))

    def test_a_write_that_names_no_row_is_refused(self):
        last = 'with no id bd acts on the task it touched last'
        for argv in (['update', '--title', 'x'], ['update', '--add-label', 'bug'], ['update', '--priority', '0'],
                     ['close'], ['close', '--reason', 'done'], ['reopen']):
            with self.subTest(argv=argv):
                self.assertIn(last, self.refusal(argv))
        self.assertIn('name the task the comment is for', self.refusal(['comments', 'add']))

    def test_a_write_whose_row_bd_chooses_is_refused(self):
        for argv in (['close', OTHER, '--claim-next'], ['close', OTHER, '--reason', 'x', '--claim-next=true'],
                     ['close', OTHER, '--claim-next=maybe']):
            with self.subTest(argv=argv):
                self.assertIn('--claim-next lets bd choose the next task', self.refusal(argv))
        self.assertIn('--continue lets bd choose the next task', self.refusal(['close', OTHER, '--continue']))

    def test_creation_from_a_file_is_refused(self):
        for argv, attachments in ((['create', '-f', 'tasks.md'], None), (['create', '--file=tasks.md'], None),
                                  (['create', '--graph', 'plan.json'], None),
                                  (['create', '@attachment:0'], {'0': {'flag': '--file', 'text': '# a\n'}})):
            with self.subTest(argv=argv):
                self.assertIn('creating several tasks from a file', self.refusal(argv, attachments))
        # A description sent as a file is one task.
        self.assertEqual(self.targets(['create', 'x', '@attachment:0'], {'0': {'flag': '--body-file', 'text': 'd'}}), [])

    def test_create_with_an_explicit_id(self):
        self.assertEqual(rc.write_targets(['create', 'x', '--id', 'pp-zz9'])['new_id'], 'pp-zz9')
        self.assertEqual(rc.write_targets(['create', 'x', '--id=pp-zz9'])['new_id'], 'pp-zz9')
        self.assertIsNone(rc.write_targets(['create', 'x'])['new_id'])
        self.assertIn('give --id once', self.refusal(['create', 'x', '--id', 'a', '--id', 'b']))

    def test_a_file_of_dependency_edges_names_its_rows(self):
        both = edges({'from': OTHER, 'to': SLOT}, {'issue_id': THIRD, 'depends_on_id': 'blocks:pp-ghi', 'type': 'blocks'})
        self.assertEqual(self.targets(['dep', 'add', '@attachment:0'], both), [OTHER, SLOT, THIRD, 'pp-ghi'])
        self.assertEqual(self.targets(['dep', 'add', '@attachment:0'], edges({'from': OTHER, 'to': 'external:p:c'})), [OTHER])
        for label, attachments in (
                ('not JSON', {'0': {'flag': '--file', 'text': 'pp-abc pp-merge-slot'}}),
                ('not an object', {'0': {'flag': '--file', 'text': '["pp-abc", "pp-merge-slot"]'}}),
                ('one end', edges({'from': OTHER})),
                ('an end that is not text', edges({'from': OTHER, 'to': 7})),
                ('too many', {'0': {'flag': '--file', 'text': '\n'.join(['{"from":"a","to":"b"}'] * 1001)}})):
            with self.subTest(body=label):
                self.assertIn('could not be read', self.refusal(['dep', 'add', '@attachment:0'], attachments))
        self.assertIn('must be the --file list of edges',
                      self.refusal(['dep', 'add', '@attachment:0'], {'0': {'flag': '--body-file', 'text': 'x'}}))
        self.assertIn('a raw --file path is not accepted', self.refusal(['dep', 'add', '--file', '/tmp/edges.jsonl']))

    def test_a_flag_that_cannot_be_resolved_refuses(self):
        for argv in (['close', '--surprise', OTHER], ['update', OTHER, '--surprise=x'], ['dep', 'add', OTHER, THIRD, '--surprise'],
                     ['dep', 'add', OTHER, THIRD, '-x']):
            with self.subTest(argv=argv):
                self.assertIn('is not one this interface can resolve to tasks', self.refusal(argv))

    def test_a_caller_written_token_is_quoted_in_a_refusal(self):
        self.assertEqual(rc.shown_token('pp-abc'), 'pp-abc')
        self.assertEqual(rc.shown_token('a\nb [2]'), '"a\\nb [2]"')
        self.assertEqual(len(rc.shown_token('x' * 500)), 60)

    def test_the_label_is_reserved_in_every_writing_spelling(self):
        for argv in (['update', OTHER, '--add-label', 'gt:slot'], ['update', OTHER, '--remove-label', 'gt:slot'],
                     ['update', OTHER, '--set-labels', 'bug,gt:slot'], ['update', OTHER, '--remove-label=gt:slot'],
                     ['create', 'x', '--labels', 'gt:slot'], ['create', 'x', '--label', 'gt:slot'],
                     ['create', 'x', '-l', 'gt:slot'], ['create', 'x', '-lgt:slot']):
            with self.subTest(argv=argv):
                self.assertEqual(rc.reserved_label_in_args(argv), 'gt:slot')
        self.assertIsNone(rc.reserved_label_in_args(['update', OTHER, '--add-label', 'gt:slots']))
        self.assertEqual(rc.first_reserved_label(['bug', 'gt:slot']), 'gt:slot')


class _Proc:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeBd:
    """bd as the endpoint calls it: `show` resolves ids as bd 1.2.2 does; anything else is a write."""

    def __init__(self, rows):
        self.rows, self.reads, self.writes = rows, [], []
        self.fail_reads = None

    def resolve(self, token):
        """Any substring of the part after the prefix, with or without the prefix (measured on real bd)."""
        found = []
        for row in self.rows:
            prefix, _, rest = row['id'].partition('-')
            if token == row['id'] or (token and token in rest) or \
                    (token.startswith(prefix + '-') and token[len(prefix) + 1:] and token[len(prefix) + 1:] in rest):
                found.append(row)
        return found

    def __call__(self, argv, **kwargs):
        argv = [str(part) for part in argv]
        if 'show' in argv:
            tokens = [token for token in argv[argv.index('show') + 1:] if not token.startswith('--')]
            self.reads.append(tokens)
            if self.fail_reads:
                return _Proc(1, self.fail_reads, 'Error: database is locked')
            found, missing = [], []
            for token in tokens:
                rows = self.resolve(token)
                if len(rows) == 1:
                    found += [row for row in rows if row not in found]
                else:
                    missing.append('Error fetching %s: no issue found matching "%s"' % (token, token))
            # As bd does: the rows it found, rc 0 when any was, the others named on stderr.
            return _Proc(0 if found else 1, json.dumps(found) if found else '', '\n'.join(missing))
        if 'list' in argv and '--id' in argv:
            wanted = argv[argv.index('--id') + 1]
            self.reads.append(['list --id', wanted])
            if self.fail_reads:
                return _Proc(1, self.fail_reads, 'Error: database is locked')
            return _Proc(0, json.dumps([row for row in self.rows if row['id'] == wanted]))
        if 'comments' in argv and 'add' not in argv:
            return _Proc(0, '[]')
        self.writes.append(argv[argv.index('--actor') + 2:])
        return _Proc(0, json.dumps({'ok': True}))


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointTests(unittest.TestCase):
    """The raw bd path of the endpoint, end to end, with bd stubbed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='slot-writes-')
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / 'bin').mkdir()
        (self.root / 'bin' / 'bd').write_text('', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text('{"password": "x", "unit": "none", "port": "1"}',
                                                           encoding='utf-8')
        (self.root / 'projects' / 'pp' / '.beads').mkdir(parents=True)
        (self.root / 'projects' / 'pp' / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        task = {'issue_type': 'task', 'status': 'open', 'labels': [], 'comments': []}
        self.bd = FakeBd([slot_row(), dict(task, id=OTHER), dict(task, id=THIRD)])

    def run_bd(self, args, actor='mallory', attachments=None):
        request = {'project': 'pp', 'actor': actor, 'action': 'bd', 'args': args}
        if attachments is not None:
            request['attachments'] = attachments
        with mock.patch.object(endpoint.subprocess, 'run', self.bd):
            return endpoint.execute(self.root, request)

    def refused(self, args, attachments=None):
        with self.assertRaises(ValueError) as caught:
            self.run_bd(args, attachments=attachments)
        self.assertEqual(self.bd.writes, [], args)
        return str(caught.exception)

    def test_every_write_on_the_slot_is_refused_and_nothing_is_written(self):
        writes = (['update', SLOT, '--title', 'mine now'], ['update', SLOT, '--description', 'x'],
                  ['update', SLOT, '--priority', '3'], ['update', SLOT, '--add-label', 'bug'],
                  ['update', SLOT, '--set-metadata', 'holder=mallory'], ['update', SLOT, '--notes', 'n'],
                  ['comments', 'add', SLOT, 'a note'], ['dep', 'add', SLOT, OTHER], ['dep', 'add', OTHER, SLOT],
                  ['dep', OTHER, '--blocks', SLOT], ['dep', 'remove', OTHER, SLOT], ['dep', 'relate', OTHER, SLOT],
                  ['create', 'a child', '--parent', SLOT, '--no-inherit-labels'],
                  ['create', 'x', '--deps', 'blocks:' + SLOT], ['update', OTHER, '--parent', SLOT],
                  # The short spellings bd resolves to the same row, the lone dash among them.
                  ['update', 'merge-slot', '--title', 'x'], ['update', 'slot', '--title', 'x'],
                  ['comments', 'add', 'erge-slo', 'a note'], ['update', OTHER, 'slot', '--priority', '1'],
                  ['update', '-', '--title', 'x'], ['comments', 'add', '-', 'a note'], ['dep', 'add', OTHER, '-'],
                  ['update', 'e-s', '--title', 'x'], ['update', 'pp-merge', '--title', 'x'])
        for args in writes:
            with self.subTest(args=args):
                said = self.refused(args)
                self.assertIn(SENTENCE, said)
                self.assertIn('Nothing but `coordinate` writes it', said)
                self.assertIn('merge-create operation repairs a damaged slot', said)

    def test_a_slot_without_its_label_is_refused_the_same(self):
        self.bd.rows[0]['labels'] = []
        self.assertIn(SENTENCE, self.refused(['update', SLOT, '--title', 'x']))
        self.assertIn(SENTENCE, self.refused(['comments', 'add', '-', 'a note']))
        self.assertIn(SENTENCE, self.refused(['update', SLOT, '--claim']))

    def test_a_file_of_edges_that_names_the_slot_is_refused(self):
        said = self.refused(['dep', 'add', '@attachment:0'], edges({'from': OTHER, 'to': SLOT}))
        self.assertIn(SENTENCE, said)
        said = self.refused(['dep', 'add', '@attachment:0'], edges({'issue_id': 'slot', 'depends_on_id': OTHER}))
        self.assertIn(SENTENCE, said)
        # One that names tasks only is written.
        self.assertEqual(self.run_bd(['dep', 'add', '@attachment:0'], attachments=edges({'from': OTHER, 'to': THIRD}))['returncode'], 0)
        self.assertEqual(len(self.bd.writes), 1)

    def test_a_write_that_names_no_row_is_refused(self):
        for args, part in ((['update', '--title', 'x'], 'with no id bd acts on the task it touched last'),
                           (['update', '--add-label', 'bug'], 'with no id bd acts on the task it touched last'),
                           (['update', '--priority', '0'], 'with no id bd acts on the task it touched last'),
                           (['comments', 'add'], 'name the task the comment is for'),
                           (['close', OTHER, '--reason', 'done', '--claim-next'], '--claim-next lets bd choose the next task'),
                           (['close', OTHER, '--continue'], '--continue lets bd choose the next task'),
                           (['create', '@attachment:0'], 'creating several tasks from a file')):
            with self.subTest(args=args):
                attachments = {'0': {'flag': '--file', 'text': '# a\n'}} if '@attachment:0' in args else None
                said = self.refused(args, attachments)
                self.assertIn(part, said)
                self.assertTrue(said.endswith('Nothing was written.'), said)
        self.assertEqual(self.bd.reads, [])

    def test_create_with_an_id_that_exists_is_refused_for_every_row(self):
        exists = 'a task with that id exists, and bd would replace its title, description, status and assignee'
        for args in (['create', 'overwritten', '--id', OTHER], ['create', 'overwritten', '--id=' + THIRD],
                     ['create', '--title', 'overwritten', '--id', OTHER, '--description', 'new']):
            with self.subTest(args=args):
                said = self.refused(args)
                self.assertIn(exists, said)
                self.assertTrue(said.endswith('Nothing was written.'))
        # The check is the first thing done, and it is exact: one read, of that id.
        self.bd.reads = []
        self.refused(['create', 'x', '--id', OTHER, '--parent', THIRD])
        self.assertEqual(self.bd.reads, [['list --id', OTHER]])

    def test_create_with_an_id_of_the_slot_is_refused(self):
        for value in (SLOT, SLOT + '.1', 'zz-merge-slot', 'zz-merge-slot.2.1'):
            with self.subTest(id=value):
                self.assertIn('that id belongs to a merge slot', self.refused(['create', 'x', '--id', value]))
        self.assertEqual(self.bd.reads, [])

    def test_create_with_an_id_that_is_not_this_projects_shape_is_refused(self):
        # bd treats ids as case-sensitive: pp-ABC would be a second row beside pp-abc. A short id is not an id.
        for value in ('pp-ABC', 'PP-abc', 'Pp-abc', 'abc', 'ab', 'zz-abc', 'pp-', 'pp', '', ' pp-abc', 'pp-abc ', 'pp-a b',
                      'pp-' + 'x' * 97, '-pp-abc', 'pp-.x'):
            with self.subTest(id=value):
                said = self.refused(['create', 'x', '--id=' + value])
                self.assertIn('an explicit id is pp-NAME in lower-case letters, digits, dots and hyphens', said)
        self.assertEqual(self.bd.reads, [])

    def test_create_with_a_new_explicit_id_is_still_allowed(self):
        # Also one that only resembles an existing id, which bd's own resolution would match.
        for value in ('pp-zz9', 'pp-ab', 'pp-abc.1', 'pp-x-merge-slot', 'pp-abcd'):
            with self.subTest(id=value):
                self.assertEqual(self.run_bd(['create', 'new', '--id', value])['returncode'], 0)
        self.assertEqual(len(self.bd.writes), 5)
        self.assertEqual(self.run_bd(['create', 'no explicit id'])['returncode'], 0)

    def test_a_failed_existence_check_refuses_the_create(self):
        self.bd.fail_reads = 'not json'
        said = self.refused(['create', 'x', '--id', 'pp-new'])
        self.assertIn('could not check whether a task with that id exists', said)

    def test_a_token_bd_cannot_resolve_refuses_the_write(self):
        for args in (['update', 'pp-nope', '--title', 'x'], ['dep', 'add', OTHER, 'pp-nope'], ['comments', 'add', 'nope', 'text'],
                     ['update', 'pp-', '--title', 'x'], ['update', OTHER, 'p', '--title', 'x']):
            with self.subTest(args=args):
                said = self.refused(args)
                self.assertIn('does not name exactly one task', said)
                self.assertIn('Name each task by its id', said)

    def test_a_value_that_is_a_flag_is_not_an_id(self):
        # `--parent` swallows the next token as its value; bd would read it as a flag of the lookup.
        said = self.refused(['update', OTHER, '--parent', '--json'])
        self.assertIn('--json is not a task id', said)
        self.assertEqual(self.bd.reads, [])

    def test_a_failed_read_refuses_the_write(self):
        self.bd.fail_reads = 'not json'
        said = self.refused(['comments', 'add', OTHER, 'a note'])
        self.assertIn('bd gave an unreadable answer', said)
        # bd exits non-zero with nothing found and no "no issue found": a failure, not an absence.
        self.bd.fail_reads = ' '
        said = self.refused(['update', OTHER, '--title', 'x'])
        self.assertIn('bd could not read the tasks (Error: database is locked)', said)

    def test_a_read_that_times_out_refuses_the_write(self):
        import subprocess

        def slow(argv, **kwargs):
            if 'show' in argv or 'list' in argv:
                raise subprocess.TimeoutExpired(argv, 60)
            return self.bd(argv, **kwargs)
        for args in (['update', OTHER, '--title', 'x'], ['create', 'x', '--id', 'pp-new']):
            with self.subTest(args=args), mock.patch.object(endpoint.subprocess, 'run', slow):
                with self.assertRaises(ValueError) as caught:
                    endpoint.execute(self.root, {'project': 'pp', 'actor': 'mallory', 'action': 'bd', 'args': args})
                self.assertIn('the read of the tasks timed out', str(caught.exception))
        self.assertEqual(self.bd.writes, [])

    def test_a_status_or_assignee_move_keeps_its_own_sentence(self):
        for args in (['update', SLOT, '--claim'], ['close', SLOT], ['reopen', SLOT], ['update', SLOT, '--status', 'open']):
            with self.subTest(args=args):
                said = self.refused(args)
                self.assertIn(SENTENCE + '. Its holder changes only through `coordinate`.', said)

    def test_the_label_is_refused_on_any_row_with_its_own_sentence(self):
        for args in (['update', OTHER, '--add-label', 'gt:slot'], ['update', OTHER, '--remove-label', 'gt:slot'],
                     ['update', SLOT, '--remove-label', 'gt:slot'], ['update', OTHER, '--set-labels', 'gt:slot'],
                     ['create', 'x', '--labels', 'gt:slot']):
            with self.subTest(args=args):
                said = self.refused(args)
                self.assertIn('The label gt:slot marks the merge slot of a project and is reserved', said)
                self.assertEqual(self.bd.reads, [])
        self.assertIn(SENTENCE, self.refused(['update', SLOT, '--set-labels', 'bug']))

    def test_an_ordinary_write_passes_with_one_read_for_all_its_rows(self):
        cases = ((['update', OTHER, '--title', 'the slot of the merge'], [[OTHER]]),
                 (['comments', 'add', OTHER, 'merge-slot'], [[OTHER]]),
                 (['dep', 'add', OTHER, THIRD], [[OTHER, THIRD]]),
                 (['update', OTHER, THIRD, '--add-label', 'bug'], [[OTHER, THIRD]]),
                 (['create', 'slot', '--description', 'merge-slot'], []),
                 (['create', 'child', '--parent', OTHER, '--no-inherit-labels'], [[OTHER]]),
                 (['dep', 'add', OTHER, 'external:proj:cap'], [[OTHER]]))
        for args, reads in cases:
            with self.subTest(args=args):
                self.bd.reads = []
                self.assertEqual(self.run_bd(args)['returncode'], 0)
                self.assertEqual(self.bd.reads, reads)
        self.assertEqual(len(self.bd.writes), len(cases))

    def test_two_tokens_for_one_row_still_pass(self):
        self.assertEqual(self.run_bd(['update', OTHER, 'abc', '--title', 'x'])['returncode'], 0)
        self.assertEqual(len(self.bd.writes), 1)

    def test_reads_are_not_guarded(self):
        for args in (['show', SLOT, '--json'], ['comments', SLOT, '--json'], ['dep', 'list', SLOT], ['ready', '--json']):
            with self.subTest(args=args):
                self.assertEqual(self.run_bd(args)['returncode'], 0)


class SlotNative:
    """The native seam of `coordinate`: one slot row, as bd keeps it."""

    def __init__(self, **row):
        self.row = slot_row(**row)
        self.updates = []

    def holder(self):
        return (self.row.get('metadata') or {}).get('holder')

    def __call__(self, argv):
        if argv[:2] == ['merge-slot', 'create']:
            return json.dumps({'id': SLOT, 'status': self.row['status']})
        if argv[:2] == ['merge-slot', 'check']:
            return json.dumps({'available': self.row['status'] == 'open', 'holder': self.holder(), 'id': SLOT,
                               'waiters': None})
        if argv[0] == 'show':
            return json.dumps([self.row if argv[1] == SLOT else {'id': argv[1], 'labels': []}])
        if argv[0] == 'update':
            self.updates.append(argv)
            if '--add-label' in argv:
                self.row['labels'] = (self.row.get('labels') or []) + [argv[argv.index('--add-label') + 1]]
            if '--status' in argv:
                self.row['status'] = argv[argv.index('--status') + 1]
            if '--assignee' in argv:
                self.row['assignee'] = argv[argv.index('--assignee') + 1] or None
            return json.dumps([self.row])
        raise AssertionError(argv)


class RepairTests(unittest.TestCase):
    """`merge-create` puts a damaged slot right; check, acquire and release say how."""

    def apply(self, native, payload, actor='alice'):
        with tempfile.TemporaryDirectory(prefix='slot-repair-') as tmp:
            return coordination.apply_native(payload, actor, native, Path(tmp))

    def test_a_sound_slot_is_left_alone(self):
        for row in ({}, {'status': 'in_progress', 'metadata': {'holder': 'bob'}}):
            native = SlotNative(**row)
            result = self.apply(native, {'operation': 'merge-create'})
            self.assertEqual((result['repaired'], native.updates), ([], []))
            self.assertEqual(result['id'], SLOT)

    def test_the_jam_of_the_review_is_repaired(self):
        # --remove-label gt:slot, then a claim: in_progress, an assignee, no label, no holder.
        native = SlotNative(labels=None, status='in_progress', assignee='mallory')
        with self.assertRaises(ValueError) as caught:
            self.apply(native, {'operation': 'merge-check'})
        self.assertEqual(str(caught.exception), coordination.SLOT_DAMAGED)
        for payload in ({'operation': 'merge-release'}, {'operation': 'merge-acquire', 'task': OTHER, 'target': 'main@1'}):
            with self.assertRaisesRegex(ValueError, 'Run the merge-create operation, which repairs it'):
                self.apply(native, payload)
        result = self.apply(native, {'operation': 'merge-create'})
        self.assertEqual(result['repaired'], ['label', 'status', 'assignee'])
        self.assertEqual(result['status'], 'open')
        self.assertEqual(native.updates, [['update', SLOT, '--add-label', 'gt:slot', '--status', 'open',
                                           '--assignee', '', '--json']])
        self.assertEqual((native.row['labels'], native.row['status'], native.row['assignee']), (['gt:slot'], 'open', None))
        self.assertTrue(self.apply(native, {'operation': 'merge-check'})['available'])
        self.assertTrue(coordination.is_merge_slot(native.row))

    def test_the_recorded_holder_decides_the_status(self):
        # Closed while held: the holder bd recorded still holds it.
        native = SlotNative(status='closed', metadata={'holder': 'bob'})
        self.assertEqual(self.apply(native, {'operation': 'merge-create'})['repaired'], ['status'])
        self.assertEqual(native.row['status'], 'in_progress')
        # Marked open while held: the same.
        native = SlotNative(status='open', metadata=json.dumps({'holder': 'bob'}))
        self.assertEqual(self.apply(native, {'operation': 'merge-create'})['repaired'], ['status'])
        self.assertEqual(native.row['status'], 'in_progress')
        # In progress with nobody holding it: free again. No holder is named by the repair.
        native = SlotNative(status='in_progress', metadata={})
        self.assertEqual(self.apply(native, {'operation': 'merge-create'})['repaired'], ['status'])
        self.assertEqual((native.row['status'], native.holder()), ('open', None))

    def test_only_the_label_missing(self):
        native = SlotNative(labels=['bug'])
        self.assertEqual(self.apply(native, {'operation': 'merge-create'})['repaired'], ['label'])
        self.assertEqual(native.updates, [['update', SLOT, '--add-label', 'gt:slot', '--json']])

    def test_a_row_that_cannot_be_read_is_not_guessed_at(self):
        native = SlotNative()
        native.row['id'] = 'pp-other'
        with self.assertRaisesRegex(ValueError, 'Could not read the merge slot row'):
            self.apply(native, {'operation': 'merge-create'})

    def test_a_child_is_not_created_under_the_slot(self):
        native = SlotNative()
        payload = {'operation': 'create-child', 'request_id': 'r-1', 'parent': SLOT, 'title': 't', 'description': 'd',
                   'type': 'task'}
        with self.assertRaises(ValueError) as caught:
            self.apply(native, payload)
        self.assertEqual(str(caught.exception), SENTENCE + '; it takes no child')


if __name__ == '__main__':
    unittest.main()
