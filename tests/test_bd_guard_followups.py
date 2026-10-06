"""Follow-ups from the kittrial-5bb.113 revision 3 review (kittrial-5bb.135).

The guard of .113 rev 3 refused every write that names the merge slot, but
``bd ready --claim`` reaches a row without naming it; ``create --id`` checked existence with
``bd list --all --id``, which does not show ephemeral or gate rows, so it replaced them; the
``--id`` shape rule allowed a trailing dot or hyphen; and an empty answer from a read counted
as "no such id". Each case is pinned here at the level it fails: the flag classification in
``tests/test_bd_write_flags.py``, the endpoint refusal here.

kittrial-5bb.138 adds two follow-ups from the .135 review: a read flag that makes bd hold the
whole project (``list --watch``, ``show ID --watch``, short ``-w``) is refused before bd starts,
and the existence check treats bd's ambiguous-prefix answer as "no row has exactly this id", so
``create --id p-ab`` with ``p-abc`` and ``p-abd`` present is allowed again while an exact id is
still refused.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import reserved_comments as rc

try:
    import endpoint
except ImportError:  # endpoint needs fcntl (POSIX)
    endpoint = None

OTHER = 'pp-abc'
WISP = 'pp-wisp-abc'
GATE = 'pp-gate-abc'


class _Proc:
    def __init__(self, returncode, stdout, stderr=''):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


class FakeBd:
    """bd 1.2.2 as the endpoint calls it: `show` sees every row class, `list --all --id` does not.

    Measured on the pinned binary (bd version 1.2.2): `create --ephemeral` and
    `create --type gate` make rows that ``list --all --id`` answers ``[]`` for while
    ``show`` returns them, and ``create TITLE --id`` then replaces the ephemeral row.
    """

    def __init__(self, rows=(), hidden=()):
        self.rows, self.hidden = list(rows), list(hidden)
        self.reads, self.writes = [], []
        self.empty_answer = False
        #: A structured error object other than the exact no-match answer, and the exit code
        #: it arrives with (kittrial-5bb.138 item absence-pins).
        self.error_answer = None
        self.error_rc = 1
        #: The exit code of the ambiguous-prefix no-match object; real bd 1.2.2 uses rc 1
        #: (kittrial-5bb.138 item rc0).
        self.no_match_rc = 1

    def all_rows(self):
        return self.rows + self.hidden

    def resolve(self, token):
        """bd resolves an id from any substring of the part after the project prefix."""
        found = []
        for row in self.all_rows():
            prefix, _, rest = row['id'].partition('-')
            if token == row['id'] or (token and token in rest) or \
                    (token.startswith(prefix + '-') and token[len(prefix) + 1:] and token[len(prefix) + 1:] in rest):
                found.append(row)
        return found

    def __call__(self, argv, **kwargs):
        argv = [str(part) for part in argv]
        # The endpoint runs `bin/bd --directory PATH --sandbox --actor ACTOR <command>`.
        command = argv[argv.index('--actor') + 2:] if '--actor' in argv else argv
        if 'show' in command:
            tokens = [token for token in command[command.index('show') + 1:] if not token.startswith('--')]
            self.reads.append(tokens)
            if self.empty_answer:
                return _Proc(0, '')            # exit 0 and no output: not an answer at all
            if self.error_answer is not None:
                # A structured error that is not the exact no-match answer (a locked database).
                return _Proc(self.error_rc, json.dumps(self.error_answer), 'Error: database is locked')
            found, missing, ambiguous = [], [], []
            for token in tokens:
                rows = self.resolve(token)
                exact = [row for row in rows if row['id'] == token]
                if exact:
                    # bd prefers an exact id over the substring resolver (measured, bd 1.2.2).
                    rows = exact
                if len(rows) > 1:
                    ambiguous.append((token, rows))
                elif rows:
                    found += [row for row in rows if row not in found]
                else:
                    missing.append('Error fetching %s: no issue found matching "%s"' % (token, token))
            if ambiguous:
                # bd 1.2.2 answers an id that is an ambiguous prefix of several rows with rc 1,
                # this structured object on stdout, and "ambiguous ID ... Use more characters to
                # disambiguate" on stderr (measured; kittrial-5bb.138 item 2).
                token, rows = ambiguous[0]
                said = ('Error fetching %s: ambiguous ID "%s" matches %d issues: [%s]\n'
                        'Use more characters to disambiguate'
                        % (token, token, len(rows), ' '.join(row['id'] for row in rows)))
                return _Proc(self.no_match_rc, json.dumps({'error': 'no issues found matching the provided IDs',
                                                           'schema_version': 1}, indent=2), said)
            return _Proc(0 if found else 1, json.dumps(found) if found else '', '\n'.join(missing))
        if 'list' in command and '--id' in command:
            # The hidden classes (`--all` shows closed rows, not ephemeral or gate rows).
            self.reads.append(['list --id', command[command.index('--id') + 1]])
            return _Proc(0, '[]')
        if 'list' in command:
            self.reads.append(command)
            return _Proc(0, json.dumps([]))
        if command[:2] == ['ready', '--claim']:
            self.writes.append(command)
            return _Proc(0, json.dumps([{'id': OTHER, 'status': 'in_progress', 'assignee': 'mallory'}]))
        if command and command[0] == 'ready':
            self.reads.append(command)
            return _Proc(0, json.dumps([{'id': OTHER, 'status': 'open'}]))
        self.writes.append(command)
        return _Proc(0, json.dumps({'ok': True}))


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointTests(unittest.TestCase):
    """The raw bd path of the endpoint, end to end, with bd stubbed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='bd-guard-followups-')
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        (self.root / 'bin').mkdir()
        (self.root / 'bin' / 'bd').write_text('', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text('{"password": "x", "unit": "none", "port": "1"}',
                                                           encoding='utf-8')
        (self.root / 'projects' / 'pp' / '.beads').mkdir(parents=True)
        (self.root / 'projects' / 'pp' / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
        task = {'issue_type': 'task', 'status': 'open', 'labels': [], 'comments': []}
        self.bd = FakeBd([dict(task, id=OTHER)],
                         [dict(task, id=WISP, ephemeral=True), dict(task, id=GATE, issue_type='gate')])

    def run_bd(self, args, attachments=None):
        request = {'project': 'pp', 'actor': 'mallory', 'action': 'bd', 'args': args}
        if attachments is not None:
            request['attachments'] = attachments
        with mock.patch.object(endpoint.subprocess, 'run', self.bd):
            return endpoint.execute(self.root, request)

    def refused(self, args):
        with self.assertRaises(ValueError) as caught:
            self.run_bd(args)
        self.assertEqual(self.bd.writes, [], args)
        return str(caught.exception)

    # 1. bd ready --claim (the P2 of the review).

    def test_ready_claim_is_refused_before_any_native_write(self):
        for args in (['ready', '--claim'], ['ready', '--claim=true', '-n', '1'], ['ready', '--claim', '--json']):
            with self.subTest(args=args):
                said = self.refused(args)
                self.assertIn('--claim lets bd choose the row it writes, which is not named in this request', said)
                self.assertTrue(said.startswith('Refusing ready: '), said)
                self.assertTrue(said.endswith('Nothing was written.'), said)
        self.assertEqual(self.bd.reads, [])
        self.assertEqual(self.bd.writes, [])

    def test_a_ready_read_is_still_a_read(self):
        for args in (['ready'], ['ready', '--json'], ['ready', '-n', '1'], ['ready', '--claim=false']):
            with self.subTest(args=args):
                self.bd.writes, self.bd.reads = [], []
                self.assertEqual(self.run_bd(args)['returncode'], 0, args)
                self.assertEqual(self.bd.writes, [], args)

    # 2. Hidden row classes: ephemeral and gate rows.

    def test_an_id_that_is_an_ephemeral_row_is_refused(self):
        said = self.refused(['create', 'replaced', '--id', WISP])
        self.assertIn('a task with that id exists, and bd would replace its title, description, status and assignee', said)
        self.assertEqual(self.bd.reads, [[WISP]])
        self.assertEqual(self.bd.writes, [])

    def test_an_id_that_is_a_gate_row_is_refused(self):
        self.assertIn('a task with that id exists', self.refused(['create', 'replaced', '--id', GATE]))

    def test_the_existence_check_does_not_use_the_list_that_hides_rows(self):
        self.refused(['create', 'replaced', '--id', WISP])
        self.assertNotIn(['list --id', WISP], self.bd.reads)
        # The check is `show`, and the read is of that id alone.
        self.assertEqual(self.bd.reads, [[WISP]])

    def test_an_id_near_an_existing_row_is_still_allowed(self):
        # bd resolves a substring, so `show` answers with rows that are not the id asked
        # for; only an exact match is the row `create --id` would replace.
        for value in ('pp-ab', 'pp-abcd', 'pp-abc.1', 'pp-x-merge-slot'):
            with self.subTest(id=value):
                self.bd.writes = []
                self.assertEqual(self.run_bd(['create', 'new', '--id', value])['returncode'], 0, value)
                self.assertEqual(len(self.bd.writes), 1, value)

    def test_an_ambiguous_prefix_of_two_ids_is_not_an_existing_id(self):
        # Real bd 1.2.2 (measured on koopa): with p-abc and p-abd present, `show p-ab` answers
        # rc 1 with the structured no-match object and "ambiguous ID ... Use more characters to
        # disambiguate". No row has exactly p-ab, so the create proceeds, exactly as the kit
        # before kittrial-5bb.135 did (kittrial-5bb.138 item 2).
        self.bd.rows.append(dict(self.bd.rows[0], id='pp-abd'))
        self.bd.writes, self.bd.reads = [], []
        self.assertEqual(self.run_bd(['create', 'new', '--id', 'pp-ab'])['returncode'], 0)
        self.assertEqual(len(self.bd.writes), 1)
        self.assertEqual(self.bd.reads, [['pp-ab']])
        # The exact id the ambiguity is near is still refused.
        self.bd.writes, self.bd.reads = [], []
        said = self.refused(['create', 'new', '--id', 'pp-abc'])
        self.assertIn('a task with that id exists', said)
        self.assertEqual(self.bd.writes, [])

    # 1b. A read flag that holds the project (`--watch`) is refused before bd starts.

    def test_watch_is_refused_before_any_native_process(self):
        for args in (['list', '--watch'], ['show', OTHER, '--watch'], ['list', '-w']):
            with self.subTest(args=args):
                self.bd.writes, self.bd.reads = [], []
                said = self.refused(args)
                self.assertIn('waits for changes', said)
                self.assertTrue(said.startswith('Refusing '), said)
                self.assertEqual(self.bd.reads, [])
                self.assertEqual(self.bd.writes, [])

    def test_a_watch_that_bd_parses_as_false_is_still_a_read(self):
        for args in (['list', '--watch=false'], ['list', '-w=false'], ['show', OTHER, '--watch=0']):
            with self.subTest(args=args):
                self.bd.writes, self.bd.reads = [], []
                self.assertEqual(self.run_bd(args)['returncode'], 0, args)
                self.assertEqual(self.bd.writes, [], args)
                self.assertTrue(self.bd.reads, args)

    def test_a_watch_inside_a_short_cluster_is_refused_before_bd_starts(self):
        # bd accepts its global booleans -q and -v in front of -w, so the guard must read every
        # letter of a short cluster, not only token[:2] (kittrial-5bb.138 item cluster). As a
        # contributor on the previous tip, `list -qw`, `list -vw` and `show ID -qw` each held the
        # project until the endpoint's 120 s timeout because only the first letter was looked at.
        for args in (['list', '-qw'], ['list', '-vw'], ['show', OTHER, '-qw'],
                     ['show', OTHER, '-vw'], ['list', '-wq']):
            with self.subTest(args=args):
                self.bd.writes, self.bd.reads = [], []
                said = self.refused(args)
                self.assertIn('waits for changes', said)
                self.assertTrue(said.startswith('Refusing '), said)
                self.assertEqual(self.bd.reads, [], args)
                self.assertEqual(self.bd.writes, [], args)

    def test_a_value_taking_letter_ends_a_short_cluster(self):
        # `-nw` is `-n w`: the rest of the token is the value, so w is not --watch. The scan
        # stops at the first value-taking letter (kittrial-5bb.138 item cluster).
        for args in (['list', '-nw'], ['list', '-n5'], ['list', '-aw'], ['list', '-qw=false']):
            with self.subTest(args=args):
                self.bd.writes, self.bd.reads = [], []
                self.assertEqual(self.run_bd(args)['returncode'], 0, args)
                self.assertEqual(self.bd.writes, [], args)
                self.assertTrue(self.bd.reads, args)

    # 3. The id shape: a trailing dot or hyphen files the row under the row it looks like.

    def test_an_id_ending_in_a_dot_or_hyphen_is_refused(self):
        for value in (OTHER + '.', OTHER + '-', 'pp-new.', 'pp-new-', 'pp-a..', 'pp-a.-'):
            with self.subTest(id=value):
                said = self.refused(['create', 'x', '--id', value])
                self.assertIn('ending in a letter or digit', said)
                self.assertTrue(said.endswith('Nothing was written.'), said)
        self.assertEqual(self.bd.reads, [])

    def test_a_dot_or_hyphen_inside_an_id_is_still_allowed(self):
        for value in ('pp-a.b', 'pp-a-b', 'pp-a.b-c'):
            with self.subTest(id=value):
                self.assertEqual(self.run_bd(['create', 'x', '--id', value])['returncode'], 0, value)

    # 4. An empty answer from a read is a failed read, not "nothing there".

    def test_an_empty_answer_from_the_existence_read_refuses_the_create(self):
        self.bd.empty_answer = True
        said = self.refused(['create', 'x', '--id', 'pp-new'])
        self.assertIn('could not check whether a task with that id exists (bd gave no answer', said)
        self.assertEqual(self.bd.writes, [])

    def test_an_empty_answer_from_a_row_read_refuses_the_write(self):
        self.bd.empty_answer = True
        said = self.refused(['update', OTHER, '--title', 'x'])
        self.assertIn('bd gave no answer', said)
        self.assertIn('so the rows this would write are not known', said)
        self.assertEqual(self.bd.writes, [])

    def test_a_read_that_says_no_issue_found_is_an_absence_not_a_failure(self):
        # bd's real answer for an id that does not exist: rc 1, "no issue found", no rows.
        self.assertEqual(self.run_bd(['create', 'x', '--id', 'pp-new'])['returncode'], 0)
        self.assertEqual(len(self.bd.writes), 1)

    # 5. The absence reading is the exact no-match object at rc 1, and only for the new-id
    # check (kittrial-5bb.138 items absence-pins and rc0).

    def test_a_structured_error_other_than_no_match_refuses_create_id(self):
        # Treating any structured error as absence would let a `database is locked` object
        # through: an existing id would be replaced and a new id created (absence-pins).
        self.bd.error_answer = {'error': 'database is locked', 'schema_version': 1}
        for value in (OTHER, 'pp-new'):
            with self.subTest(id=value):
                self.bd.writes, self.bd.reads = [], []
                said = self.refused(['create', 'x', '--id', value])
                self.assertIn('could not check whether a task with that id exists', said)
                self.assertEqual(self.bd.writes, [], value)

    def test_the_no_match_answer_is_an_absence_only_for_the_new_id_check(self):
        # The no-match object at rc 1 is an absence only when the caller asks for it, as
        # _guard_new_id does; every other guard read refuses it (absence-pins).
        self.bd.rows.append(dict(self.bd.rows[0], id='pp-abd'))
        path = self.root / 'projects' / 'pp'
        with mock.patch.object(endpoint.subprocess, 'run', self.bd):
            rows, said = endpoint._bd_read(self.root, path, 'mallory', ['show', 'pp-ab', '--json'])
            self.assertIsNone(rows, 'a guard read that is not the new-id check must refuse the no-match answer')
            self.assertIn('ambiguous ID', said)
            rows, said = endpoint._bd_read(self.root, path, 'mallory', ['show', 'pp-ab', '--json'],
                                           resolver_absence=True)
            self.assertEqual(rows, [])

    def test_a_no_match_object_at_exit_code_zero_refuses_the_create(self):
        # The real bd 1.2.2 answers the no-match object with rc 1; rc 0 with an error object is
        # a failed read, not an absence (kittrial-5bb.138 item rc0).
        self.bd.rows.append(dict(self.bd.rows[0], id='pp-abd'))
        self.bd.no_match_rc = 0
        self.bd.writes, self.bd.reads = [], []
        said = self.refused(['create', 'new', '--id', 'pp-ab'])
        self.assertIn('could not check whether a task with that id exists', said)
        self.assertEqual(self.bd.writes, [])
        # With bd's real rc 1 the same answer is an absence and the create proceeds.
        self.bd.no_match_rc = 1
        self.assertEqual(self.run_bd(['create', 'new', '--id', 'pp-ab'])['returncode'], 0)
        self.assertEqual(len(self.bd.writes), 1)

    # The unit-level classification the endpoint relies on.

    def test_ready_claim_is_classified_as_a_write_and_the_other_reads_are_not(self):
        for argv in (['ready', '--claim'], ['ready', '--claim=true'], ['ready', '--claim=1']):
            with self.subTest(argv=argv):
                self.assertTrue(rc.write_targets(argv)['refusal'], argv)
        for argv in (['ready'], ['ready', '--json'], ['ready', '--claim=false'], ['ready', '--claim=0'],
                     ['list', '--all'], ['show', OTHER], ['search', 'x'], ['count'], ['state'], ['lint'],
                     ['comments', OTHER], ['dep', 'list'], ['dep', 'tree'], ['dep', 'cycles']):
            with self.subTest(argv=argv):
                self.assertIsNone(rc.write_targets(argv), argv)

    def test_watch_is_classified_as_a_hold(self):
        for argv in (['list', '--watch'], ['show', OTHER, '--watch'], ['list', '-w'], ['list', '-wq'],
                     ['list', '-qw'], ['list', '-vw'], ['show', OTHER, '-qw'], ['show', OTHER, '-vw']):
            with self.subTest(argv=argv):
                self.assertTrue(rc.write_targets(argv)['refusal'], argv)
        for argv in (['list', '--watch=false'], ['show', OTHER, '--watch=false'], ['list', '-w=false'],
                     ['list'], ['show', OTHER], ['list', '-nw'], ['list', '-qw=false']):
            with self.subTest(argv=argv):
                self.assertIsNone(rc.write_targets(argv), argv)


if __name__ == '__main__':
    unittest.main()
