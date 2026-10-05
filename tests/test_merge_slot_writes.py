"""No contributor write reaches the merge slot, its label is reserved, and a damaged slot is repaired.

Review 01a109cc of kittrial-5bb.113: only status and assignee moves on the slot were
refused over SSH. Title, description, priority, comments, labels and dependencies were all
writable, and one ``--remove-label gt:slot`` put the slot back among claimable work; a
claim then jammed it (acquire and release both refused) until host bd repaired it.
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
SENTENCE = SLOT + " is the project's merge slot, an internal record, not a task"


def slot_row(**changes):
    row = {'id': SLOT, 'title': 'Merge Slot', 'issue_type': 'task', 'status': 'open', 'assignee': None,
           'labels': ['gt:slot'], 'metadata': None, 'comments': []}
    row.update(changes)
    return row


class CandidateTests(unittest.TestCase):
    """Which tokens could name the slot. bd resolves any substring of the part after the prefix."""

    def test_what_real_bd_resolves_to_the_slot_is_a_candidate(self):
        # Measured on bd 1.2.2: each of these reached alpha-merge-slot.
        for token in ('alpha-merge-slot', 'merge-slot', 'slot', 'merge', 'alpha-merge', 'erge-slo', 'lot', 't',
                      'my-proj-merge-slot', 'my-proj-merge', 'MERGE-SLOT'):
            with self.subTest(token=token):
                self.assertTrue(rc.could_name_merge_slot(token))

    def test_an_ordinary_id_is_not(self):
        for token in ('pp-abc', 'abc', 'pp-5bb.107', '5bb', 'alpha', 'pp-merge-slot.1', 'merge-slots', 'x-slot-y',
                      '', '-merge-slot', '--slot', None, 7):
            with self.subTest(token=token):
                self.assertFalse(rc.could_name_merge_slot(token))

    def test_the_id_positions_of_each_writing_command(self):
        cases = [
            (['update', SLOT, '--title', 'mine'], [SLOT]),
            (['update', 'slot', '-p', '0'], ['slot']),
            (['update', OTHER, '--title', 'slot'], []),                      # a title is not an id
            (['update', OTHER, '--description', 'merge-slot'], []),
            (['update', OTHER, '--parent', SLOT], [SLOT]),
            (['update', OTHER, '--parent=' + SLOT], [SLOT]),
            (['update', OTHER, SLOT, '--priority', '1'], [SLOT]),
            (['close', SLOT, '--reason', 'slot'], [SLOT]),
            (['reopen', 'merge-slot'], ['merge-slot']),
            (['create', 'slot', '--parent', OTHER], []),                     # the title of a new task
            (['create', 'a child', '--parent', SLOT, '--no-inherit-labels'], [SLOT]),
            (['create', 'x', '--deps', 'blocks:' + SLOT + ',' + OTHER], [SLOT]),
            (['create', 'x', '--deps', OTHER + ',slot'], ['slot']),
            (['create', 'x', '--waits-for', SLOT], [SLOT]),
            (['comments', 'add', SLOT, 'a note'], [SLOT]),
            (['comments', 'add', OTHER, 'slot'], []),                        # a body is not an id
            (['comments', SLOT], []),                                        # a read
            (['dep', 'add', SLOT, OTHER], [SLOT]),
            (['dep', 'add', OTHER, SLOT, '-t', 'blocks'], [SLOT]),
            (['dep', 'remove', OTHER, 'merge-slot'], ['merge-slot']),
            (['dep', 'rm', SLOT, OTHER], [SLOT]),
            (['dep', 'relate', OTHER, SLOT], [SLOT]),
            (['dep', OTHER, '--blocks', SLOT], [SLOT]),
            (['dep', OTHER, '--blocks=' + SLOT], [SLOT]),
            (['dep', OTHER, '-b', SLOT], [SLOT]),
            (['dep', OTHER, '-b' + SLOT], [SLOT]),
            (['dep', 'add', OTHER, '--blocked-by', SLOT], [SLOT]),
            (['dep', 'add', OTHER, '--depends-on', SLOT], [SLOT]),
            (['dep', 'list', SLOT], []),                                     # reads
            (['dep', 'tree', SLOT], []),
            (['dep', 'cycles'], []),
            (['show', SLOT], []),
            (['list', '--label', 'gt:slot'], []),
            (['update', OTHER, '--title', 'x'], []),
            (['comments', 'add', OTHER, 'hello'], []),
            (['dep', 'add', OTHER, 'pp-def'], []),
        ]
        for argv, expected in cases:
            with self.subTest(argv=argv):
                self.assertEqual(rc.merge_slot_candidates(argv), expected)

    def test_an_unknown_flag_cannot_hide_a_target(self):
        self.assertEqual(rc.merge_slot_candidates(['close', '--surprise', SLOT]), [SLOT])
        self.assertEqual(rc.merge_slot_candidates(['update', '--surprise=' + SLOT, OTHER]), [SLOT])
        self.assertEqual(rc.merge_slot_candidates([]), [])
        self.assertEqual(rc.merge_slot_candidates('update slot'), [])

    def test_the_label_is_reserved_in_every_writing_spelling(self):
        self.assertEqual(rc.MERGE_SLOT_LABEL, coordination.MERGE_SLOT_LABEL)
        self.assertEqual(rc.MERGE_SLOT_ID_PART, coordination.MERGE_SLOT_SUFFIX.lstrip('-'))
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
    """bd as the endpoint calls it: `show` resolves an id as bd 1.2.2 does; anything else is a write."""

    def __init__(self, rows):
        self.rows, self.reads, self.writes = rows, [], []
        self.fail_reads = None

    def resolve(self, token):
        found = []
        for row in self.rows:
            prefix, _, rest = row['id'].partition('-')
            if token == row['id'] or token in rest or (token.startswith(prefix + '-') and token[len(prefix) + 1:] in rest):
                found.append(row)
        return found

    def __call__(self, argv, **kwargs):
        argv = [str(part) for part in argv]
        if 'show' in argv:
            tokens = [token for token in argv[argv.index('show') + 1:] if not token.startswith('--')]
            self.reads.append(tokens)
            if self.fail_reads:
                return _Proc(1, '', self.fail_reads)
            found = []
            for token in tokens:
                rows = self.resolve(token)
                if len(rows) != 1:
                    return _Proc(1, '', 'Error fetching %s: no issue found matching "%s"' % (token, token))
                found += rows
            return _Proc(0, json.dumps(found))
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
        self.bd = FakeBd([slot_row(), {'id': OTHER, 'issue_type': 'task', 'status': 'open', 'labels': [],
                                       'comments': []}])

    def run_bd(self, args, actor='mallory'):
        with mock.patch.object(endpoint.subprocess, 'run', self.bd):
            return endpoint.execute(self.root, {'project': 'pp', 'actor': actor, 'action': 'bd', 'args': args})

    def refused(self, args):
        with self.assertRaises(ValueError) as caught:
            self.run_bd(args)
        self.assertEqual(self.bd.writes, [], args)
        return str(caught.exception)

    def test_every_write_on_the_slot_is_refused_and_nothing_is_written(self):
        writes = (['update', SLOT, '--title', 'mine now'], ['update', SLOT, '--description', 'x'],
                  ['update', SLOT, '--priority', '3'], ['update', SLOT, '--add-label', 'bug'],
                  ['update', SLOT, '--set-metadata', 'holder=mallory'], ['update', SLOT, '--notes', 'n'],
                  ['comments', 'add', SLOT, 'a note'], ['dep', 'add', SLOT, OTHER], ['dep', 'add', OTHER, SLOT],
                  ['dep', OTHER, '--blocks', SLOT], ['dep', 'remove', OTHER, SLOT],
                  ['create', 'a child', '--parent', SLOT, '--no-inherit-labels'],
                  ['create', 'x', '--deps', 'blocks:' + SLOT], ['update', OTHER, '--parent', SLOT],
                  # The short spellings bd resolves to the same row.
                  ['update', 'merge-slot', '--title', 'x'], ['update', 'slot', '--title', 'x'],
                  ['comments', 'add', 'erge-slo', 'a note'], ['update', OTHER, 'slot', '--priority', '1'])
        for args in writes:
            with self.subTest(args=args):
                said = self.refused(args)
                self.assertIn(SENTENCE, said)
                self.assertIn('Nothing but `coordinate` writes it', said)
                self.assertIn('merge-create operation repairs a damaged slot', said)

    def test_a_status_or_assignee_move_keeps_its_own_sentence(self):
        for args in (['update', SLOT, '--claim'], ['close', SLOT], ['reopen', SLOT], ['update', SLOT, '--status', 'open']):
            with self.subTest(args=args):
                said = self.refused(args)
                self.assertIn(SENTENCE + '. Its holder changes only through `coordinate`.', said)

    def test_the_label_is_refused_on_any_row_with_its_own_sentence(self):
        for args in (['update', OTHER, '--add-label', 'gt:slot'], ['update', OTHER, '--remove-label', 'gt:slot'],
                     ['update', SLOT, '--remove-label', 'gt:slot'], ['update', OTHER, '--set-labels', 'gt:slot'],
                     ['create', 'x', '--labels', 'gt:slot'], ['create', 'x-merge-slot', '--id', 'pp-x-merge-slot',
                                                              '--labels', 'gt:slot']):
            with self.subTest(args=args):
                said = self.refused(args)
                self.assertIn('The label gt:slot marks the merge slot of a project and is reserved', said)
                self.assertEqual(self.bd.reads, [])
        # Replacing the labels of the slot without naming the label is a write on the slot.
        self.assertIn(SENTENCE, self.refused(['update', SLOT, '--set-labels', 'bug']))

    def test_an_ordinary_write_passes_and_costs_no_read(self):
        for args in (['update', OTHER, '--title', 'the slot of the merge'], ['comments', 'add', OTHER, 'merge-slot'],
                     ['dep', 'add', OTHER, 'pp-def'], ['create', 'slot', '--description', 'merge-slot'],
                     ['update', OTHER, '--add-label', 'bug']):
            with self.subTest(args=args):
                self.assertEqual(self.run_bd(args)['returncode'], 0)
        self.assertEqual(len(self.bd.writes), 5)
        self.assertEqual(self.bd.reads, [])

    def test_a_candidate_that_is_another_task_costs_one_read_and_passes(self):
        self.bd.rows.append({'id': 'pp-lot', 'issue_type': 'task', 'status': 'open', 'labels': [], 'comments': []})
        self.assertEqual(self.run_bd(['update', 'pp-lot', '--title', 'x'])['returncode'], 0)
        self.assertEqual((self.bd.reads, len(self.bd.writes)), ([['pp-lot']], 1))

    def test_a_candidate_bd_cannot_resolve_is_left_to_bd(self):
        # bd finds no row for it and refuses the write itself; the guard does not answer for bd.
        self.assertEqual(self.run_bd(['update', 'pp-zzz-slot', '--title', 'x'])['returncode'], 0)
        self.assertEqual((self.bd.reads, len(self.bd.writes)), ([['pp-zzz-slot']], 1))

    def test_a_failed_read_refuses_the_write(self):
        self.bd.fail_reads = 'Error: database is locked'
        said = self.refused(['comments', 'add', SLOT, 'a note'])
        self.assertIn('Could not check whether %s names the merge slot' % SLOT, said)
        self.assertIn('database is locked', said)

    def test_a_slot_that_lost_its_label_is_writable_so_that_it_can_be_put_right(self):
        # Only host bd can remove the label now; the row is then an ordinary one until merge-create repairs it.
        self.bd.rows[0]['labels'] = []
        self.assertEqual(self.run_bd(['update', SLOT, '--title', 'x'])['returncode'], 0)

    def test_reads_of_the_slot_are_not_writes(self):
        for args in (['show', SLOT, '--json'], ['comments', SLOT, '--json'], ['dep', 'list', SLOT]):
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
