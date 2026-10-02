"""Reference catalog records, slice 1 (kittrial-5bb.66; docs/REFERENCE_CATALOG_DESIGN.md).

The fake bd mirrors pinned bd 1.2.2 as verified on a disposable database: `list
--label X` matches the exact label (every `--label` must match; `--label-any a,b`
needs at least one; `--id a,b` returns only the rows that exist) and prints no
comments, `show ID... --json` prints rows (comments only with `--include-comments`)
and fails only when every id is missing, `export --all` prints every row with its
comments, a comment's `author` is the acting `--actor`, and `close` sets the status.
"""
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import admin
import keyed_records
import reference_records as rr
import reserved_comments
from requirements import content_hash

TODAY = time.struct_time((2026, 10, 1, 12, 0, 0, 3, 274, 0))
OPERATOR = 'ops-james'


class RefNative:
    def __init__(self):
        self.rows = []
        self.calls = []
        self.counter = 0
        self.comment_counter = 0
        self.actor = 'alice'
        self.create_outcome = 'ok'
        self.close_outcome = 'ok'
        self.fail_comment_prefix = None
        self.unrelated = 0   # rows an export also carries, to make its cost visible

    def row(self, task):
        return next(row for row in self.rows if row['id'] == task)

    def seed(self, task, labels=(), issue_type='task', status='open', comments=()):
        row = dict(id=task, title='Seeded ' + task, description='', issue_type=issue_type,
                   status=status, labels=list(labels), comments=list(comments))
        self.rows.append(row)
        return row

    def add_comment(self, task, body, author=None):
        self.comment_counter += 1
        comment = dict(id='c-%d' % self.comment_counter, text=body, author=author or self.actor,
                       created_at='2026-10-01T12:00:00Z')
        self.row(task)['comments'].append(comment)
        return comment

    @staticmethod
    def _bare(row):
        return {key: value for key, value in row.items() if key != 'comments'}

    def __call__(self, args):
        self.calls.append(list(args))
        command = args[0]
        if command == 'export':
            return ''.join(json.dumps(row) + '\n' for row in self.rows)
        if command == 'list':
            rows = self.rows
            for index, token in enumerate(args):
                if token == '--label':
                    rows = [row for row in rows if args[index + 1] in row['labels']]
                elif token == '--label-any':
                    wanted = args[index + 1].split(',')
                    rows = [row for row in rows if any(label in row['labels'] for label in wanted)]
                elif token == '--id':
                    wanted = args[index + 1].split(',')
                    rows = [row for row in rows if row['id'] in wanted]
            return json.dumps([self._bare(row) for row in rows])
        if command == 'show':
            ids = [token for token in args[1:] if not token.startswith('--')]
            found = [copy.deepcopy(row) if '--include-comments' in args else self._bare(row)
                     for row in self.rows if row['id'] in ids]
            if not found:
                raise ValueError('no issues found matching the provided IDs')
            return json.dumps(found)
        if command == 'create':
            if '--dry-run' in args:
                return json.dumps({'dry_run': True})
            if self.create_outcome == 'not-written':
                raise RuntimeError('create not written')
            self.counter += 1
            task = 'ref-%d' % self.counter
            labels = args[args.index('--labels') + 1].split(',')
            self.rows.append(dict(id=task, title=args[args.index('--title') + 1],
                                  description=args[args.index('--description') + 1], issue_type='task',
                                  status='open', labels=labels, comments=[]))
            if self.create_outcome == 'lost-response':
                raise RuntimeError('create committed, response lost')
            return json.dumps({'id': task})
        if command == 'close':
            if self.close_outcome == 'fail':
                raise ValueError('native close failed')
            if self.close_outcome == 'crash':
                raise RuntimeError('the process died before the close')
            self.row(args[1])['status'] = 'closed'
            return json.dumps([{'id': args[1], 'status': 'closed'}])
        if command == 'comments' and args[1] == 'add':
            task, body = args[2], args[3]
            if self.fail_comment_prefix and body.startswith(self.fail_comment_prefix):
                raise ValueError('native comment failed')
            comment = self.add_comment(task, body)
            return json.dumps({'id': comment['id'], 'author': comment['author'], 'text': body})
        if command == 'update':
            row = self.row(args[1])
            if '--add-label' in args:
                label = args[args.index('--add-label') + 1]
                if label not in row['labels']:
                    row['labels'].append(label)
            if '--remove-label' in args:
                label = args[args.index('--remove-label') + 1]
                row['labels'] = [value for value in row['labels'] if value != label]
            return json.dumps({'id': args[1]})
        raise AssertionError('unexpected native command: %r' % (args,))

    def writes(self):
        return [call for call in self.calls
                if call[0] in ('create', 'close', 'comments', 'update') and '--dry-run' not in call]


def entry(**extra):
    payload = {'schema_version': 1, 'operation_id': 'alex-ref-1', 'operation': 'propose',
               'key': 'calendar.trading', 'title': 'Trading calendar authority',
               'statement': 'Charts derive holidays from the pinned calendar module.',
               'authority': {'type': 'repo-path', 'path': 'src/example/calendar.py',
                             'commit': '0' * 40, 'anchor': 'HOLIDAYS'},
               'owner': 'account:u-0001', 'review_by': '2027-01-15', 'tags': ['calendar', 'data'],
               'decisions': [], 'revision': 1, 'expected_sha256': None}
    payload.update(extra)
    return {name: value for name, value in payload.items() if value is not ...}


def acceptance(**extra):
    data = {'decision_id': 'decision-42', 'owners': ['account:u-0001'], 'approvers': ['account:u-0001'],
            'policy': 'any-owner', 'evidence': 'decision decision-42'}
    data.update(extra)
    return data


class ReferenceCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.native = RefNative()
        clock = patch('time.gmtime', return_value=TODAY)
        clock.start()
        self.addCleanup(clock.stop)

    def propose(self, **extra):
        self.native.actor = 'alice'
        return rr.apply_native(entry(**extra), 'alice', self.native, self.project)

    def revise(self, **extra):
        self.native.actor = 'alice'
        fields = dict(operation='revise', operation_id='alex-ref-2', revision=2, statement='Revised statement.')
        fields.update(extra)
        return rr.apply_native(entry(**fields), 'alice', self.native, self.project)

    def accept(self, revision, digest, actor=OPERATOR, operators=(OPERATOR,), **extra):
        self.native.actor = actor
        payload = {'schema_version': 1, 'operation_id': 'owner-apply-%d' % revision, 'operation': 'accept',
                   'key': 'calendar.trading', 'revision': revision, 'record_sha256': digest,
                   'acceptance_state': 'accepted', 'acceptance': acceptance()}
        payload.update(extra)
        return rr.apply_native(payload, actor, self.native, self.project, operator=True,
                               operators=list(operators))

    def get(self, key='calendar.trading', operators=(OPERATOR,)):
        return rr.read(['get', key], self.native, list(operators))

    def sha(self, revision, key='calendar.trading'):
        row, _ = rr.anchor_for(rr.read_key_rows(self.native, key), key)
        return rr.existing_revisions(row)[revision]['sha256']


class ProposeAndReviseTests(ReferenceCase):
    def test_propose_creates_a_closed_labelled_anchor_and_a_draft_revision_1(self):
        result = self.propose()
        self.assertEqual({k: result[k] for k in ('key', 'revision', 'native_id', 'state', 'created', 'reconciled')},
                         {'key': 'calendar.trading', 'revision': 1, 'native_id': 'ref-1', 'state': 'draft',
                          'created': True, 'reconciled': False})
        row = self.native.row('ref-1')
        self.assertEqual(row['status'], 'closed')
        identity = content_hash({'operation_id': 'alex-ref-1'})
        self.assertTrue({'reference', 'reference:draft', 'reference-key:calendar-trading',
                         'request:' + identity} <= set(row['labels']))
        self.assertTrue(any(label.startswith('request-content:') for label in row['labels']))
        self.assertNotIn('Charts derive', row['title'] + row['description'])
        self.assertEqual(len(row['comments']), 1)
        self.assertEqual(result['record_comment_id'], row['comments'][0]['id'])
        self.assertTrue(row['comments'][0]['text'].startswith('Kind: reference-entry-v1\n'))
        # The order of the native writes: create, close, then the record.
        writes = [call[0] for call in self.native.writes()]
        self.assertEqual(writes[:3], ['create', 'close', 'comments'])
        # The writer's anchor is exactly what every slice-0 surface hides (rollback to ec5d659).
        self.assertTrue(reserved_comments.is_record_anchor(row))
        self.assertEqual(reserved_comments.hide_records([row]), [])

    def reads(self):
        # (verb, number of ids named): a list or an export is one read; a show names ids.
        return [(call[0], len([token for token in call[1:] if not token.startswith('--')])
                 if call[0] == 'show' else 1)
                for call in self.native.calls if call[0] in ('list', 'show', 'export')]

    def seed_catalog(self, count):
        """`count` more entries, written as the writer leaves them, without the per-entry cost."""
        for index in range(count):
            key = 'bulk.k%03d' % index
            record = rr.entry_record(entry(key=key), 1, 'draft')
            self.native.seed('bulk-%d' % index, labels=['reference', 'reference:draft', rr.key_label(key)],
                             status='closed', comments=[{'id': 'b%d' % index, 'text': rr.entry_comment(record),
                                                         'author': 'alice'}])

    def test_get_and_the_writes_read_only_their_own_key_whatever_the_catalog_size(self):
        # Review 01a0fbfd read-write-cost: at 236 entries get took 10 s and propose 23 s
        # because both read the whole catalog (propose twice).
        self.seed_catalog(240)
        self.native.calls = []
        self.assertEqual(self.get('bulk.k150')['state'], 'draft-only')
        self.assertEqual(self.reads(), [('list', 1), ('show', 1)])
        self.native.calls = []
        self.propose()
        # One preflight read (a list; nothing to show yet), then only the new anchor.
        self.assertEqual(self.reads(), [('list', 1), ('show', 1)])
        self.assertEqual([call[0] for call in self.native.writes()], ['create', 'close', 'comments'])
        digest = self.sha(1)
        self.native.calls = []
        self.revise(expected_sha256=digest)
        self.assertEqual(self.reads(), [('list', 1), ('show', 1)])
        digest = self.sha(2)
        self.native.calls = []
        self.accept(2, digest)
        self.assertEqual(self.reads(), [('list', 1), ('show', 1)])
        self.assertFalse([call for call in self.native.calls if call[0] == 'export'])

    def test_the_whole_catalog_read_is_one_show_when_small_and_one_export_above_the_threshold(self):
        self.seed_catalog(rr.CATALOG_SHOW_MAX)
        self.native.calls = []
        self.assertEqual(rr.read(['list', '--limit', '100'], self.native, [OPERATOR])['total'], rr.CATALOG_SHOW_MAX)
        self.assertEqual(self.reads(), [('list', 1), ('show', rr.CATALOG_SHOW_MAX)])
        self.seed_catalog_more = self.native.seed('bulk-x', labels=['reference', 'reference:draft',
                                                                    rr.key_label('bulk.extra')], status='closed',
                                                  comments=[{'id': 'bx', 'author': 'alice', 'text': rr.entry_comment(
                                                      rr.entry_record(entry(key='bulk.extra'), 1, 'draft'))}])
        self.native.seed('ordinary-task')
        self.native.calls = []
        listing = rr.read(['list', '--limit', '100'], self.native, [OPERATOR])
        self.assertEqual(listing['total'], rr.CATALOG_SHOW_MAX + 1)
        self.assertEqual([call[0] for call in self.native.calls], ['list', 'export'])

    def test_receipts_pass_the_frozen_slice0_schema(self):
        self.propose()
        files = {'.reference-requests/' + path.name: json.loads(path.read_text(encoding='utf-8'))
                 for path in (self.project / '.reference-requests').glob('*.json')}
        self.assertEqual(len(files), 1)
        admin.validate_coordination_files(files)
        self.assertEqual(next(iter(files.values()))['status'], 'complete')

    def test_an_identical_retry_writes_nothing_more(self):
        first = self.propose()
        before = len(self.native.writes())
        again = self.propose()
        self.assertEqual(len(self.native.writes()), before)
        self.assertEqual((again['native_id'], again['record_comment_id']),
                         (first['native_id'], first['record_comment_id']))

    def test_revise_is_compare_and_swap_on_revision_and_hash(self):
        self.propose()
        digest = self.sha(1)
        for extra, message in (({'revision': 3, 'expected_sha256': digest}, 'must write revision 2'),
                               ({'expected_sha256': 'f' * 64}, 'expected_sha256 does not match')):
            before = len(self.native.writes())
            with self.assertRaisesRegex(ValueError, message):
                self.revise(**extra)
            self.assertEqual(len(self.native.writes()), before)
        result = self.revise(expected_sha256=digest)
        self.assertEqual((result['revision'], result['state']), (2, 'draft'))

    def test_duplicate_keys_and_slug_collisions_are_refused_before_any_write(self):
        self.propose()
        before = len(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.propose(operation_id='other-1')
        self.assertEqual(len(self.native.writes()), before)
        self.propose(operation_id='other-3', key='calendar.trading-x')
        before = len(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'collides'):
            self.propose(operation_id='other-4', key='calendar.trading.x')
        # Revising the colliding key finds no entry of its own.
        with self.assertRaisesRegex(ValueError, 'Unknown reference key calendar.trading.x'):
            self.revise(operation_id='other-5', key='calendar.trading.x', expected_sha256=self.sha(1))
        self.assertEqual(len(self.native.writes()), before)

    def test_field_refusals_write_nothing(self):
        cases = [
            ({'owner': 'person:session-4e40fde3'}, 'not a session actor'),
            ({'owner': 'alex/session1'}, 'durable identity'),
            ({'authority': {'type': 'repo-path', 'path': '/etc/passwd'}}, 'relative'),
            ({'authority': {'type': 'repo-path', 'path': 'src/../secret'}}, 'relative'),
            ({'authority': {'type': 'url', 'url': 'http://example.invalid/x', 'retrieved': '2026-09-01'}}, 'https'),
            ({'authority': {'type': 'url', 'url': 'https://u:p@example.invalid/x', 'retrieved': '2026-09-01'}},
             'userinfo'),
            ({'authority': {'type': 'url', 'url': 'https://example.invalid/x', 'retrieved': '2026-12-01'}},
             'later than today'),
            ({'review_by': '2029-01-01'}, 'at most 24 months'),
            ({'review_by': '2026-02-30'}, 'real calendar date'),
            ({'tags': ['Bad Tag']}, 'slugs'),
            ({'key': 'Calendar'}, 'lowercase dotted'),
            ({'title': 'x' * 201}, 'at most 200'),
            ({'decisions': ['nope-1']}, 'Unknown decision link\\(s\\) nope-1'),
            ({'acceptance_state': 'accepted'}, 'never caller-supplied'),
            ({'sha256': 'a' * 64}, 'never caller-supplied'),
            ({'successor': 'x.y'}, 'never caller-supplied'),
            ({'acceptance': acceptance()}, 'never caller-supplied'),
            ({'labels': ['reference:accepted']}, 'Labels are controlled'),
            ({'revision': 2}, 'creates revision 1'),
            ({'expected_sha256': 'a' * 64}, 'must be null'),
            ({'surprise': 1}, 'unknown field'),
        ]
        for extra, message in cases:
            with self.subTest(extra=extra):
                with self.assertRaisesRegex(ValueError, message):
                    self.propose(**extra)
        self.assertEqual(self.native.writes(), [])
        self.assertFalse((self.project / '.reference-requests').exists())

    def test_a_past_review_date_is_allowed_on_a_draft_and_decisions_must_resolve(self):
        self.native.seed('dec-1', issue_type='decision')
        self.native.seed('dec-2', labels=['decision'])
        result = self.propose(review_by='2026-01-01', decisions=['dec-1', 'dec-2'])
        self.assertEqual(result['state'], 'draft')
        with self.assertRaisesRegex(ValueError, 'Unknown decision link.*task-9'):
            self.native.seed('task-9')
            self.propose(operation_id='other', key='calendar.other', decisions=['dec-1', 'task-9'])


class AcceptanceTests(ReferenceCase):
    def test_only_an_allowlisted_operator_accepts_and_refusal_reserves_nothing(self):
        self.propose()
        before = len(self.native.writes())
        for actor, operators, message in (('mallory', [OPERATOR], 'not a server-side configured operator'),
                                          (OPERATOR, [], 'No operator allowlist')):
            with self.assertRaisesRegex(ValueError, message):
                self.accept(1, self.sha(1), actor=actor, operators=operators)
        self.assertEqual(len(self.native.writes()), before)
        self.assertEqual(len(list((self.project / '.reference-requests').glob('*.json'))), 1)

    def test_accept_writes_evidence_then_the_next_revision_then_the_label(self):
        self.propose()
        digest = self.sha(1)
        self.native.calls = []
        result = self.accept(1, digest)
        self.assertEqual((result['revision'], result['state']), (2, 'accepted'))
        writes = self.native.writes()
        self.assertTrue(writes[0][3].startswith('Kind: reference-acceptance-v1\n'))
        self.assertTrue(writes[1][3].startswith('Kind: reference-entry-v1\n'))
        self.assertEqual(writes[2:], [['update', 'ref-1', '--remove-label', 'reference:draft', '--json'],
                                      ['update', 'ref-1', '--add-label', 'reference:accepted', '--json']])
        view = self.get()
        self.assertEqual((view['state'], view['record']['revision'], view['due']), ('accepted', 2, 'ok'))
        self.assertEqual(view['acceptance']['operator'], OPERATOR)
        self.assertEqual(view['acceptance']['record_sha256'], view['record']['sha256'])
        # Identical content: only revision and acceptance_state differ from the reviewed draft.
        row, _ = rr.anchor_for(rr.read_rows(self.native), 'calendar.trading')
        revisions = rr.existing_revisions(row)
        differ = {name for name in revisions[1] if revisions[1][name] != revisions[2][name]}
        self.assertEqual(differ, {'revision', 'acceptance_state', 'sha256'})

    def test_accept_retry_is_idempotent_and_a_stale_or_wrong_hash_is_refused(self):
        self.propose()
        digest = self.sha(1)
        self.accept(1, digest)
        before = len(self.native.writes())
        again = self.accept(1, digest)
        self.assertEqual(len(self.native.writes()), before)
        self.assertTrue(again['reconciled'])
        self.revise(operation_id='alex-ref-3', revision=3, expected_sha256=self.sha(2))
        before = len(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'not the newest'):
            self.accept(2, self.sha(2), operation_id='owner-apply-stale')
        with self.assertRaisesRegex(ValueError, 'record_sha256 does not match'):
            self.accept(3, 'f' * 64, operation_id='owner-apply-wrong')
        self.assertEqual(len(self.native.writes()), before)

    def test_reaccepting_an_accepted_entry_writes_a_new_revision_and_evidence(self):
        self.propose()
        self.accept(1, self.sha(1))
        result = self.accept(2, self.sha(2), acceptance=acceptance(decision_id='decision-43'))
        self.assertEqual((result['revision'], result['state']), (3, 'accepted'))
        view = self.get()
        self.assertEqual((view['record']['revision'], view['acceptance']['decision_id']), (3, 'decision-43'))

    def test_a_contributor_revision_of_an_accepted_entry_is_proposed_not_accepted(self):
        self.propose()
        self.accept(1, self.sha(1))
        self.revise(operation_id='alex-ref-3', revision=3, expected_sha256=self.sha(2))
        view = self.get()
        self.assertEqual((view['state'], view['record']['revision'], view['proposed']['revision']),
                         ('accepted', 2, 3))
        self.assertIn('reference:accepted', self.native.row('ref-1')['labels'])

    def test_direct_accepted_revision_1(self):
        self.native.actor = OPERATOR
        payload = dict(entry(operation='draft', operation_id='owner-direct'), acceptance_state='accepted',
                       acceptance=acceptance())
        result = rr.apply_native(payload, OPERATOR, self.native, self.project, operator=True,
                                 operators=[OPERATOR])
        self.assertEqual((result['revision'], result['state'], result['created']), (1, 'accepted', True))
        self.assertEqual(self.get()['state'], 'accepted')
        self.assertIn('reference:accepted', self.native.row('ref-1')['labels'])
        with self.assertRaisesRegex(ValueError, 'operation must be one of propose, revise'):
            rr.apply_native(dict(payload, operation_id='sneaky'), 'alice', self.native, self.project)

    def test_accepted_revisions_need_a_future_review_date_and_a_pinned_commit(self):
        self.propose(review_by='2026-01-01')
        with self.assertRaisesRegex(ValueError, 'in the past'):
            self.accept(1, self.sha(1))
        self.propose(operation_id='other', key='calendar.other',
                     authority={'type': 'repo-path', 'path': 'src/x.py'})
        with self.assertRaisesRegex(ValueError, 'pinned commit'):
            self.accept(1, self.sha(1, 'calendar.other'), key='calendar.other', operation_id='owner-x')

    def test_an_operator_removed_from_the_allowlist_makes_the_acceptance_inert_not_silent(self):
        self.propose()
        self.accept(1, self.sha(1))
        view = self.get(operators=())
        self.assertEqual((view['state'], view['acceptance'], view['acceptance_inert'], view['inert_operator']),
                         ('draft-only', None, True, OPERATOR))
        inert = [warning for warning in view['warnings'] if warning['code'] == 'acceptance-inert'][0]
        # The wording covers both a removed operator and an author who never was one.
        self.assertIn('who is not on the deployment operator allowlist', inert['detail'])
        self.assertNotIn('no longer', inert['detail'])
        rows = rr.read_rows(self.native)
        approver = rr.work_attention(rows, 'ops-new', ['ops-new'])
        self.assertEqual((approver['acceptance_inert'], approver['unset'], approver['total']), (1, 0, 1))
        self.assertEqual(approver['items'][0]['due'], 'acceptance-inert')
        self.assertEqual(approver['items'][0]['inert_operator'], OPERATOR)
        # Re-adding the operator restores the same evidence with no rewrite.
        self.assertEqual(self.get(operators=(OPERATOR,))['state'], 'accepted')

    def test_evidence_authored_by_another_actor_is_not_an_acceptance(self):
        self.propose()
        self.accept(1, self.sha(1))
        for comment in self.native.row('ref-1')['comments']:
            if comment['text'].startswith('Kind: reference-acceptance-v1\n'):
                comment['author'] = 'mallory'
        view = self.get(operators=(OPERATOR, 'mallory'))
        self.assertEqual(view['state'], 'draft-only')
        # The item names the stored native author, not the name the payload claims.
        self.assertEqual(self.get(operators=(OPERATOR,))['inert_operator'], 'mallory')


class CrashAndReconcileTests(ReferenceCase):
    def test_a_lost_create_response_is_finished_by_the_same_operation(self):
        self.native.create_outcome = 'lost-response'
        with self.assertRaises(RuntimeError):
            self.propose()
        row = self.native.row('ref-1')
        self.assertEqual((row['status'], row['comments']), ('open', []))
        self.assertFalse(reserved_comments.is_record_anchor(row))   # an ordinary row until finished
        listing = rr.read(['list'], self.native, [OPERATOR])
        self.assertEqual(listing['total'], 0)
        self.assertIn('1 incomplete anchor with no record yet (ref-1)', listing['coverage'])
        with self.assertRaisesRegex(ValueError, 'no revision record yet'):
            self.get()
        with self.assertRaisesRegex(ValueError, 'interrupted propose by another operation'):
            self.propose(operation_id='someone-else')
        self.native.create_outcome = 'ok'
        result = self.propose()
        self.assertEqual((result['native_id'], result['reconciled']), ('ref-1', True))
        self.assertEqual(self.native.row('ref-1')['status'], 'closed')
        self.assertEqual(self.get()['state'], 'draft-only')

    def test_a_crash_between_create_and_close_is_finished_by_the_retry(self):
        # Review 01a0fbfd (d): the process dies after the create committed and before
        # the close. The row is open, labelled and has no record: an ordinary task.
        self.native.close_outcome = 'crash'
        with self.assertRaises(RuntimeError):
            self.propose()
        row = self.native.row('ref-1')
        self.assertEqual((row['status'], row['comments']), ('open', []))
        self.assertFalse(reserved_comments.is_record_anchor(row))
        receipt = json.loads(next((self.project / '.reference-requests').glob('*.json')).read_text(encoding='utf-8'))
        self.assertEqual((receipt['status'], receipt['id'], receipt['created']), ('pending', 'ref-1', True))
        self.assertIn('1 incomplete anchor', rr.read(['list'], self.native, [OPERATOR])['coverage'])
        with self.assertRaisesRegex(ValueError, 'interrupted propose by another operation'):
            self.propose(operation_id='someone-else')
        self.native.close_outcome = 'ok'
        self.native.calls = []
        result = self.propose()
        self.assertEqual((result['native_id'], result['created']), ('ref-1', False))
        self.assertEqual([call[0] for call in self.native.writes()], ['close', 'comments'])
        self.assertEqual(self.native.row('ref-1')['status'], 'closed')
        self.assertTrue(reserved_comments.is_record_anchor(self.native.row('ref-1')))
        self.assertEqual(len([row for row in self.native.rows if 'reference' in row['labels']]), 1)

    def test_a_failed_close_is_finished_by_the_retry(self):
        self.native.close_outcome = 'fail'
        with self.assertRaises(ValueError):
            self.propose()
        self.native.close_outcome = 'ok'
        self.propose()
        self.assertEqual(self.native.row('ref-1')['status'], 'closed')
        self.assertTrue(reserved_comments.is_record_anchor(self.native.row('ref-1')))

    def test_reconcile_refuses_to_complete_an_anchor_without_its_record(self):
        self.native.fail_comment_prefix = 'Kind: reference-entry-v1'
        with self.assertRaisesRegex(ValueError, 'admin.py reference-reconcile'):
            self.propose()
        with self.assertRaisesRegex(ValueError, 'has no reference revision record yet'):
            rr.reconcile(self.project, 'alex-ref-1', OPERATOR, 'checked', 'complete', self.native,
                         issue_id='ref-1')
        self.native.fail_comment_prefix = None
        self.assertEqual(self.propose()['native_id'], 'ref-1')   # the retry finishes the anchor
        receipt = next((self.project / '.reference-requests').glob('*.json'))
        self.assertEqual(json.loads(receipt.read_text(encoding='utf-8'))['status'], 'complete')

    def test_a_create_that_never_happened_can_be_released(self):
        self.native.create_outcome = 'not-written'
        with self.assertRaises(RuntimeError):
            self.propose()
        released = rr.reconcile(self.project, 'alex-ref-1', OPERATOR, 'nothing created', 'released',
                                self.native)
        self.assertEqual(released['status'], 'released')
        self.native.create_outcome = 'ok'
        self.assertTrue(self.propose()['created'])


class ReadIsolationTests(ReferenceCase):
    def seed_entries(self):
        self.propose()
        self.accept(1, self.sha(1))
        self.propose(operation_id='b-1', key='feed.units', owner='person:bob', review_by=None, tags=['data'])
        self.propose(operation_id='c-1', key='identity.registry', review_by='2026-10-20', tags=['identity'],
                     authority={'type': 'url', 'url': 'https://example.invalid/x', 'retrieved': '2026-09-01'})
        self.native.actor = OPERATOR
        rr.apply_native({'schema_version': 1, 'operation_id': 'acc-c', 'operation': 'accept',
                         'key': 'identity.registry', 'revision': 1,
                         'record_sha256': self.sha(1, 'identity.registry'), 'acceptance_state': 'accepted',
                         'acceptance': acceptance()}, OPERATOR, self.native, self.project, operator=True,
                        operators=[OPERATOR])
        # An ordinary project task that merely uses the word as a label stays out of the catalog.
        self.native.seed('task-1', labels=['reference'])

    def test_list_orders_filters_and_pages(self):
        self.seed_entries()
        listing = rr.read(['list'], self.native, [OPERATOR])
        self.assertEqual([(item['key'], item['due'], item['state']) for item in listing['items']],
                         [('identity.registry', 'due-soon', 'accepted'), ('feed.units', 'unset', 'draft-only'),
                          ('calendar.trading', 'ok', 'accepted')])
        self.assertEqual(rr.read(['list', '--tag', 'data', '--state', 'accepted'], self.native,
                                 [OPERATOR])['total'], 1)
        self.assertEqual([item['key'] for item in rr.read(['list', '--owner', 'person:bob'], self.native,
                                                         [OPERATOR])['items']], ['feed.units'])
        self.assertEqual(rr.read(['list', '--due', 'due-soon'], self.native, [OPERATOR])['total'], 1)
        page = rr.read(['list', '--limit', '1', '--offset', '1'], self.native, [OPERATOR])
        self.assertEqual((page['total'], len(page['items']), page['next_offset']), (3, 1, 2))
        for bad in (['--limit', '0'], ['--limit', '101'], ['--state', 'open'], ['--due', 'soon'],
                    ['--owner', 'session-4e40fde3'], ['--tag'], ['--colour', 'x']):
            with self.assertRaises(ValueError):
                rr.read(['list', *bad], self.native, [OPERATOR])

    def test_one_malformed_or_unsupported_entry_fails_only_itself(self):
        self.seed_entries()
        self.native.add_comment('ref-2', 'Kind: reference-entry-v1\n{"not": "valid"}')
        self.native.add_comment('ref-3', 'Kind: reference-entry-v2\n{}')
        listing = rr.read(['list'], self.native, [OPERATOR])
        self.assertEqual([item['key'] for item in listing['items']], ['calendar.trading'])
        self.assertIn('1 entry skipped as malformed (ref-2)', listing['coverage'])
        self.assertIn('1 entry skipped as unsupported (ref-3)', listing['coverage'])
        self.assertEqual(self.get('feed.units')['state'], 'malformed')
        self.assertEqual(self.get('identity.registry')['state'], 'unsupported')
        attention = rr.work_attention(rr.read_rows(self.native), OPERATOR, [OPERATOR])
        self.assertEqual(attention['malformed'], 2)
        with self.assertRaisesRegex(ValueError, 'does not support'):
            rr.apply_native(entry(operation='revise', operation_id='r-x', key='identity.registry', revision=2,
                                  expected_sha256=self.sha(1, 'identity.registry')),
                            'alice', self.native, self.project)

    def test_work_attention_counts_for_everyone_and_items_for_approvers(self):
        self.seed_entries()
        rows = rr.read_rows(self.native)
        worker = rr.work_attention(rows, 'session-4e40fde3-3b27-4efd-87c6-40642aafa8c6', [OPERATOR])
        self.assertEqual({k: worker[k] for k in ('expired', 'due_soon', 'unset', 'acceptance_inert', 'total')},
                         {'expired': 0, 'due_soon': 1, 'unset': 1, 'acceptance_inert': 0, 'total': 2})
        self.assertEqual((worker['items'], worker['truncated']), ([], True))
        approver = rr.work_attention(rows, OPERATOR, [OPERATOR])
        self.assertEqual([item['key'] for item in approver['items']], ['identity.registry'])

    def test_brief_attention_is_bounded_trust_marked_and_carries_no_statement(self):
        self.seed_entries()
        rows = rr.read_rows(self.native)
        brief = rr.brief_attention(rows, {'labels': ['calendar']}, [OPERATOR])
        self.assertEqual([item['key'] for item in brief['attention']], ['identity.registry', 'calendar.trading'])
        self.assertEqual({item['trust'] for item in brief['attention']}, {'accepted'})
        self.assertNotIn('Charts derive', json.dumps(brief))
        self.assertEqual((brief['attention_total'], brief['attention_more']), (2, None))

    def test_due_classes(self):
        current = rr.date(2026, 10, 1)
        self.assertEqual([rr.due(value, current) for value in (None, '2026-09-30', '2026-10-01', '2026-10-31',
                                                              '2026-11-01')],
                         ['unset', 'expired', 'due-soon', 'due-soon', 'ok'])


class WorkAndBriefIntegrationTests(ReferenceCase):
    """`work` and `brief` read the export they already take; reference rows are attention, never work."""

    def setUp(self):
        super().setUp()
        self.propose(review_by='2026-10-10')
        self.accept(1, self.sha(1))
        self.propose(operation_id='b-1', key='feed.units', owner='person:bob', review_by=None, tags=['data'])
        self.native.seed('task-1', labels=['calendar'])
        self.native.row('task-1')['assignee'] = 'alice'

    def export(self, argv):
        assert argv == ['export', '--all']
        return '\n'.join(json.dumps(row) for row in self.native.rows) + '\n'

    def work(self, actor, *args):
        import work
        return work.execute(self.project, actor, 'work', list(args), {}, self.export, operators=[OPERATOR])

    def test_work_carries_counts_for_everyone_and_items_for_an_approver(self):
        worker = self.work('alice', '--mine')
        self.assertEqual([item['task'] for item in worker['items']], ['task-1'])
        block = worker['attention']['reference_review']
        self.assertEqual({k: block[k] for k in ('due_soon', 'unset', 'total')}, {'due_soon': 1, 'unset': 1, 'total': 2})
        self.assertEqual(block['items'], [])
        approver = self.work(OPERATOR)['attention']['reference_review']
        self.assertEqual([item['key'] for item in approver['items']], ['calendar.trading'])
        self.assertNotIn('ref-1', [item['task'] for item in self.work(OPERATOR)['items']])
        with self.assertRaisesRegex(ValueError, '--ref-limit'):
            self.work('alice', '--ref-limit', '0')
        import work
        self.assertIn('--ref-limit', work.help_payload('work')['usage'])

    def test_brief_shows_tagged_and_due_entries_with_trust(self):
        import briefing
        result = briefing.brief(self.native.rows, 'p', 'task-1', operators=[OPERATOR])
        self.assertEqual([(item['key'], item['due'], item['trust']) for item in result['attention']],
                         [('calendar.trading', 'due-soon', 'accepted')])
        self.assertNotIn('Charts derive', json.dumps(result['attention']))
        self.assertIn('Reference review [due-soon, accepted]', briefing.format_brief(result))


if __name__ == '__main__':
    unittest.main()
