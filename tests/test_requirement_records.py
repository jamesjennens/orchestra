import copy
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import client
import requirement_records as rr
import reserved_comments
from export_requirements import REVISION_PREFIX, parse_json
from requirements import content_hash


class Native:
    """Stateful native seam: exported rows plus create/comment/update writes.

    `--dry-run` is a read-only preflight: it validates the parent and returns
    without writing, exactly like the pinned bd. `writes()` counts only real
    mutations so a refusal can be checked for zero native writes.
    """

    def __init__(self):
        self.calls = []
        self.rows = []
        self.counter = 0
        self.actor = 'alice'
        self.create_outcome = 'ok'
        self.comment_outcome = 'ok'
        self.update_outcome = 'ok'

    def seed(self, task, title='Seeded record', description='Seeded body', labels=None,
             comments=None, issue_type='task'):
        existing = next((row for row in self.rows if row['id'] == task), None)
        if existing is not None:
            return existing
        row = dict(id=task, title=title, description=description, issue_type=issue_type,
                   status='open', labels=list(labels or []), comments=list(comments or []))
        self.rows.append(row)
        return row

    def row(self, task):
        return next(row for row in self.rows if row['id'] == task)

    def __call__(self, args):
        self.calls.append(list(args))
        if args[0] == 'export':
            return '\n'.join(json.dumps(row) for row in self.rows) + '\n'
        if args[0] == 'create':
            parent = args[args.index('--parent') + 1] if '--parent' in args else None
            known = {row['id'] for row in self.rows}
            if parent is not None and parent not in known:
                raise ValueError('parent not found: ' + parent)
            if '--dry-run' in args:
                return json.dumps({'dry_run': True})
            if self.create_outcome == 'fail-after-preflight':
                raise ValueError('native create failed after preflight')
            if self.create_outcome == 'not-written':
                raise RuntimeError('create not written')
            self.counter += 1
            task = 'req-%d' % self.counter
            labels = args[args.index('--labels') + 1].split(',') if '--labels' in args else []
            row = dict(id=task, title=args[args.index('--title') + 1],
                       description=args[args.index('--description') + 1],
                       issue_type='task', status='open', labels=labels, comments=[])
            if parent is not None:
                row['parent'] = parent
            self.rows.append(row)
            if self.create_outcome == 'lost-response':
                raise RuntimeError('create committed, response lost')
            return json.dumps({'id': task})
        if args[0] == 'comments':
            if args[1] == 'add':
                task, body = args[2], args[3]
                if self.comment_outcome == 'fail':
                    raise ValueError('native comment failed')
                if self.comment_outcome == 'not-written':
                    raise RuntimeError('comment not written')
                row = self.row(task)
                comment_id = str(len(row['comments']) + 1)
                row['comments'].append(dict(id=comment_id, text=body, author=self.actor,
                                            created_at='2026-09-25T00:00:00Z'))
                if self.comment_outcome == 'lost-response':
                    raise RuntimeError('comment committed, response lost')
                return json.dumps({'id': comment_id})
            return json.dumps(self.row(args[1])['comments'])
        if args[0] == 'update':
            if self.update_outcome == 'fail':
                raise ValueError('native update failed')
            if self.update_outcome == 'not-written':
                raise RuntimeError('update not written')
            row = self.row(args[1])
            if '--add-label' in args:
                label = args[args.index('--add-label') + 1]
                if label not in row['labels']:
                    row['labels'].append(label)
            if '--remove-label' in args:
                label = args[args.index('--remove-label') + 1]
                row['labels'] = [value for value in row['labels'] if value != label]
            return json.dumps({'id': args[1]})
        raise AssertionError('unexpected native command: ' + repr(args))

    def count(self, *prefix):
        return sum(call[:len(prefix)] == list(prefix) for call in self.calls)

    def writes(self, *prefix):
        """Real mutating calls only; `--dry-run` preflights are excluded."""
        prefix = list(prefix)
        result = []
        for call in self.calls:
            if '--dry-run' in call:
                continue
            if not prefix:
                if call[0] in ('create', 'comments', 'update'):
                    result.append(call)
            elif call[:len(prefix)] == prefix:
                result.append(call)
        return result

    def comments(self, task):
        return [c for c in self.row(task)['comments'] if c['text'].startswith(REVISION_PREFIX)]

    def record(self, task, revision):
        for comment in self.comments(task):
            record = parse_json(comment['text'][len(REVISION_PREFIX):])
            if record['revision'] == revision:
                return record
        raise AssertionError('no revision %d on %s' % (revision, task))


class RequirementRecordTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.native = Native()
        self.native.seed('job-1')

    def apply(self, payload, actor='alice', operator=False):
        return rr.apply_native(copy.deepcopy(payload), actor, self.native, self.project,
                               operator=operator)

    def _without_none(self, payload):
        for name in ('task', 'parent', 'key', 'revision', 'title', 'description', 'acceptance'):
            if name in payload and payload[name] is None:
                payload.pop(name)
        return payload

    def draft(self, **extra):
        payload = dict(schema_version=1, operation_id='op-1', operation='draft',
                       kind='requirement', title='R01: Intent', key='R01',
                       description='Statement of intent.', acceptance_state='draft',
                       parent='job-1')
        payload.update(extra)
        return self._without_none(payload)

    def revise(self, **extra):
        payload = dict(schema_version=1, operation_id='op-2', operation='revise',
                       kind='requirement', task='req-1', title='R01: Intent', key='R01',
                       description='Revised statement.', revision=2,
                       acceptance_state='draft')
        payload.update(extra)
        return self._without_none(payload)

    def acceptance(self, **extra):
        data = {'owners': ['owner-a'], 'approvers': ['owner-a'], 'policy': 'any-owner',
                'decision_id': 'decision-1', 'evidence': 'review-1'}
        data.update(extra)
        return data

    def accept(self, **extra):
        payload = self.revise(operation_id='op-accept', acceptance_state='accepted',
                              description='Accepted statement.', acceptance=self.acceptance())
        payload.update(extra)
        return self._without_none(payload)

    def receipts(self):
        journal = self.project / '.requirement-requests'
        return sorted(journal.glob('*.json')) if journal.is_dir() else []

    # --- draft ---------------------------------------------------------

    def test_draft_creates_record_with_controlled_labels_and_one_comment(self):
        result = self.apply(self.draft())
        self.assertEqual(result['id'], 'req-1')
        self.assertTrue(result['created'])
        self.assertFalse(result['reconciled'])
        labels = set(self.native.row('req-1')['labels'])
        self.assertLessEqual({'requirement', 'requirement:draft'}, labels)
        self.assertEqual(len(self.native.comments('req-1')), 1)
        record = self.native.record('req-1', 1)
        self.assertEqual(record['id'], 'req-1')
        self.assertEqual(record['key'], 'R01')
        self.assertEqual(record['acceptance_state'], 'draft')
        self.assertEqual(record['sha256'], content_hash(record))
        # the posted body is a schema-valid supported revision record
        self.assertEqual(reserved_comments.parse_requirement_record(
            self.native.comments('req-1')[0]['text']), record)
        create = next(call for call in self.native.writes('create'))
        self.assertIn('--no-inherit-labels', create)
        self.assertEqual(create[create.index('--parent') + 1], 'job-1')
        self.assertEqual(create[create.index('--type') + 1], 'task')
        # a dry-run preflight precedes the single real create
        self.assertEqual(self.native.count('create'), 2)
        self.assertEqual(len(self.native.writes('create')), 1)

    def test_draft_retry_is_idempotent(self):
        first = self.apply(self.draft())
        second = self.apply(self.draft())
        self.assertEqual(second['id'], first['id'])
        self.assertTrue(second['reconciled'])
        self.assertFalse(second['created'])
        self.assertEqual(len(self.native.writes('create')), 1)
        self.assertEqual(len(self.native.comments('req-1')), 1)

    def test_draft_selects_typed_existing_record_without_creating(self):
        self.native.seed('req-x', labels=['requirement'])
        result = self.apply(self.draft(operation_id='op-select', task='req-x', parent=None))
        self.assertEqual(result['id'], 'req-x')
        self.assertFalse(result['created'])
        self.assertEqual(self.native.writes('create'), [])
        labels = set(self.native.row('req-x')['labels'])
        self.assertLessEqual({'requirement', 'requirement:draft'}, labels)
        self.assertEqual(self.native.record('req-x', 1)['id'], 'req-x')

    def test_draft_refuses_untyped_ordinary_task_and_epic(self):
        # Reproduces the reviewer probes: bob's ordinary task and an epic job
        # must not be relabelled as requirement/requirement:draft.
        self.native.seed('pp-550', title="bob's ordinary task")
        self.native.seed('pp-q2u', title='epic job', issue_type='epic')
        for task in ('pp-550', 'pp-q2u'):
            with self.subTest(task=task):
                before = list(self.native.writes())
                with self.assertRaisesRegex(ValueError, 'not a requirement/brd-section record'):
                    self.apply(self.draft(operation_id='sel-' + task, task=task, parent=None))
                self.assertEqual(self.native.writes(), before)
                self.assertEqual(self.native.row(task)['labels'], [])
                self.assertEqual(self.native.comments(task), [])

    def test_draft_rejects_unknown_task_and_ambiguous_parent(self):
        with self.assertRaisesRegex(ValueError, 'Unknown requirement record'):
            self.apply(self.draft(task='missing', parent=None))
        with self.assertRaisesRegex(ValueError, 'not both'):
            self.apply(self.draft(task='req-x'))
        with self.assertRaisesRegex(ValueError, 'needs a parent job'):
            self.apply(self.draft(parent=None))

    def test_draft_brd_section_uses_narrative_type_label_without_key(self):
        self.apply(self.draft(operation_id='op-narrative', kind='brd-section', key=None,
                              title='Purpose', description='Narrative section.'))
        labels = set(self.native.row('req-1')['labels'])
        self.assertLessEqual({'brd-section', 'requirement:draft'}, labels)
        self.assertNotIn('key', self.native.record('req-1', 1))
        with self.assertRaisesRegex(ValueError, 'must not carry a requirement key'):
            self.apply(self.draft(operation_id='op-narrative-2', kind='brd-section'))

    def test_draft_requires_draft_acceptance_state_and_revision_one(self):
        with self.assertRaisesRegex(ValueError, 'acceptance_state must be draft'):
            self.apply(self.draft(acceptance_state='accepted'))
        with self.assertRaisesRegex(ValueError, 'revision 1'):
            self.apply(self.draft(revision=2))
        self.assertEqual(self.native.writes('create'), [])

    # --- revise --------------------------------------------------------

    def test_revise_posts_next_revision_and_keeps_draft_state(self):
        self.apply(self.draft())
        result = self.apply(self.revise())
        self.assertEqual(result['revision'], 2)
        labels = set(self.native.row('req-1')['labels'])
        self.assertIn('requirement:draft', labels)
        self.assertNotIn('requirement:accepted', labels)
        self.assertEqual(len(self.native.comments('req-1')), 2)
        self.assertEqual(self.native.record('req-1', 2)['acceptance_state'], 'draft')
        self.assertEqual(len(self.native.writes('create')), 1)

    def test_revise_retry_reconciles_and_bad_revisions_are_refused(self):
        self.apply(self.draft())
        self.apply(self.revise())
        retry = self.apply(self.revise())
        self.assertTrue(retry['reconciled'])
        self.assertEqual(len(self.native.comments('req-1')), 2)
        for payload, message in (
                (self.revise(operation_id='op-gap', revision=4), 'must write revision 3'),
                (self.revise(operation_id='op-back', revision=1, description='Rewritten past'), 'different content'),
                (self.revise(operation_id='op-conflict', revision=2, description='Conflicting two'), 'different content')):
            with self.subTest(payload=payload['operation_id']):
                before = len(self.native.comments('req-1'))
                with self.assertRaisesRegex(ValueError, message):
                    self.apply(payload)
                self.assertEqual(len(self.native.comments('req-1')), before)

    def test_revise_requires_an_existing_revision(self):
        self.native.seed('req-x', labels=['requirement'])
        with self.assertRaisesRegex(ValueError, 'use draft first'):
            self.apply(self.revise(operation_id='op-bare', task='req-x', revision=1))
        self.assertEqual(self.native.comments('req-x'), [])

    def test_revise_requires_task_and_revision(self):
        with self.assertRaisesRegex(ValueError, 'needs the existing requirement task id'):
            self.apply(self.revise(operation_id='op-no-task', task=None))
        with self.assertRaisesRegex(ValueError, 'needs the next revision number'):
            self.apply(self.revise(operation_id='op-no-rev', revision=None))

    def test_revise_refuses_cross_kind_swap(self):
        # Reproduces the reviewer probe: kind=brd-section on a keyed requirement
        # swapped the type label and dropped the key.
        self.apply(self.draft())
        before = list(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'cross-kind'):
            self.apply(self.revise(operation_id='op-kind', kind='brd-section', key=None,
                                   revision=2))
        self.assertEqual(self.native.writes(), before)
        self.assertLessEqual({'requirement', 'requirement:draft'},
                             set(self.native.row('req-1')['labels']))
        self.assertEqual(self.native.record('req-1', 1)['key'], 'R01')
        self.assertFalse(any(label.startswith('brd-section')
                             for label in self.native.row('req-1')['labels']))

    def test_revise_refuses_key_swap(self):
        self.apply(self.draft())
        before = list(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'key is bound'):
            self.apply(self.revise(operation_id='op-key', key='R99', revision=2))
        self.assertEqual(self.native.writes(), before)

    def test_requirement_keys_are_unique(self):
        self.apply(self.draft(operation_id='key-1'))
        before = list(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'keys must be unique'):
            self.apply(self.draft(operation_id='key-2', key='R01'))
        self.assertEqual(self.native.writes(), before)

    # --- contributor drafts only, operator accepts with F3 evidence ----

    def test_contributor_cannot_accept_and_reserves_nothing(self):
        self.apply(self.draft())
        before = list(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'Contributors may only draft'):
            self.apply(self.accept(), actor='alice')
        self.assertEqual(self.native.writes(), before)
        self.assertEqual(self.native.row('req-1')['labels'].count('requirement:accepted'), 0)

    def test_contributor_cannot_demote_an_accepted_record(self):
        self.apply(self.draft())
        self.apply(self.accept(), operator=True)
        self.assertLessEqual({'requirement', 'requirement:accepted'},
                             set(self.native.row('req-1')['labels']))
        before = list(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'demote'):
            self.apply(self.revise(operation_id='op-demote', revision=3), actor='mallory')
        self.assertEqual(self.native.writes(), before)
        labels = set(self.native.row('req-1')['labels'])
        self.assertIn('requirement:accepted', labels)
        self.assertNotIn('requirement:draft', labels)

    def test_operator_accepts_with_f3_evidence(self):
        self.apply(self.draft())
        result = self.apply(self.accept(), operator=True)
        self.assertEqual(result['revision'], 2)
        self.assertEqual(result['acceptance_state'], 'accepted')
        labels = set(self.native.row('req-1')['labels'])
        self.assertIn('requirement:accepted', labels)
        self.assertNotIn('requirement:draft', labels)
        record = self.native.record('req-1', 2)
        self.assertEqual(record['acceptance_state'], 'accepted')
        self.assertEqual(result['acceptance']['manifest_sha256'], record['sha256'])
        self.assertEqual(result['acceptance']['owners'], ['owner-a'])

    def test_operator_accept_without_evidence_is_refused(self):
        self.apply(self.draft())
        before = list(self.native.writes())
        payload = self.revise(operation_id='op-no-evidence', revision=2,
                              acceptance_state='accepted')
        with self.assertRaisesRegex(ValueError, 'F3 acceptance evidence'):
            self.apply(payload, operator=True)
        self.assertEqual(self.native.writes(), before)

    def test_operator_accept_rejects_incomplete_evidence(self):
        self.apply(self.draft())
        before = list(self.native.writes())
        payload = self.accept(operation_id='op-bad-evidence',
                              acceptance=self.acceptance(owners=[]))
        with self.assertRaisesRegex(ValueError, 'invalid acceptance evidence'):
            self.apply(payload, operator=True)
        self.assertEqual(self.native.writes(), before)

    def test_operator_demotion_also_requires_evidence(self):
        self.apply(self.draft())
        self.apply(self.accept(), operator=True)
        with self.assertRaisesRegex(ValueError, 'F3 acceptance evidence'):
            self.apply(self.revise(operation_id='op-demote-1', revision=3), operator=True)
        result = self.apply(self.revise(operation_id='op-demote-2', revision=3,
                                        acceptance=self.acceptance()), operator=True)
        self.assertEqual(result['acceptance_state'], 'draft')
        self.assertIn('requirement:draft', self.native.row('req-1')['labels'])

    # --- controlled labels and refused fields --------------------------

    def test_arbitrary_labels_are_refused_before_any_native_write(self):
        payloads = (self.draft(labels=['review-ready']),
                    self.draft(add_labels=['requirement:evil']),
                    self.draft(labels=[]))
        for payload in payloads:
            with self.subTest(payload=sorted(payload)):
                with self.assertRaisesRegex(ValueError, 'arbitrary label'):
                    self.apply(payload)
        with self.assertRaisesRegex(ValueError, 'arbitrary label'):
            rr.backfill(dict(schema_version=1, operation_id='bf-labels', labels=['x'],
                             records=[dict(task='req-1', kind='requirement', acceptance_state='draft')]),
                        'operator', self.native, self.project)
        with self.assertRaisesRegex(ValueError, 'kind must be one of'):
            self.apply(self.draft(kind='requirement:evil'))
        self.assertEqual(self.native.calls, [])

    def test_arbitrary_state_label_is_replaced_not_trusted(self):
        self.native.seed('req-x', labels=['requirement', 'requirement:accepted', 'triage'])
        # A contributor may not demote an accepted record; the operator path
        # replaces a stale accepted label only with evidence.
        with self.assertRaisesRegex(ValueError, 'demote'):
            self.apply(self.draft(operation_id='op-flip', task='req-x', parent=None))
        self.assertEqual(set(self.native.row('req-x')['labels']),
                         {'requirement', 'requirement:accepted', 'triage'})
        self.apply(self.draft(operation_id='op-flip-op', task='req-x', parent=None,
                              acceptance=self.acceptance()), operator=True)
        labels = set(self.native.row('req-x')['labels'])
        self.assertIn('requirement:draft', labels)
        self.assertNotIn('requirement:accepted', labels)
        # unrelated labels are left alone: the operation only manages controlled ones
        self.assertIn('triage', labels)

    def test_decided_by_is_refused_with_the_proposal_pointer(self):
        with self.assertRaisesRegex(ValueError, 'kittrial-pth.25'):
            self.apply(self.draft(decided_by=[dict(kind='decision', id='d-1')]))
        self.assertEqual(self.native.calls, [])

    # --- idempotency and uncertain outcomes ----------------------------

    def test_operation_id_reuse_with_different_content_is_refused(self):
        self.apply(self.draft())
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.apply(self.draft(description='A different statement.'))
        self.assertEqual(len(self.native.writes('create')), 1)

    def test_lost_create_response_reconciles_without_duplicate(self):
        self.native.seed('job-1')
        self.native.create_outcome = 'lost-response'
        with self.assertRaises(RuntimeError):
            self.apply(self.draft())
        self.native.create_outcome = 'ok'
        result = self.apply(self.draft())
        self.assertTrue(result['reconciled'])
        self.assertEqual(len(self.native.writes('create')), 1)
        self.assertEqual(len(self.native.rows), 2)  # job-1 + req-1
        self.assertEqual(len(self.native.comments('req-1')), 1)

    def test_lost_create_without_native_record_fails_closed(self):
        self.native.create_outcome = 'not-written'
        with self.assertRaises(RuntimeError):
            self.apply(self.draft())
        self.native.create_outcome = 'ok'
        with self.assertRaisesRegex(ValueError, 'outcome uncertain'):
            self.apply(self.draft())
        self.assertEqual(len(self.native.writes('create')), 1)
        self.assertEqual([row['id'] for row in self.native.rows], ['job-1'])

    def test_lost_comment_response_reconciles_without_duplicate(self):
        self.apply(self.draft())
        self.native.comment_outcome = 'lost-response'
        with self.assertRaises(RuntimeError):
            self.apply(self.revise())
        self.native.comment_outcome = 'ok'
        result = self.apply(self.revise())
        self.assertTrue(result['reconciled'])
        self.assertEqual(len(self.native.comments('req-1')), 2)

    def test_malformed_existing_revision_comment_fails_closed(self):
        self.native.seed('req-x', labels=['requirement', 'requirement:draft'],
                         comments=[dict(id='bad', text=REVISION_PREFIX + '{not json',
                                        author='other', created_at='2026-09-25T00:00:00Z')])
        with self.assertRaisesRegex(ValueError, 'malformed requirement revision comment'):
            self.apply(self.draft(operation_id='op-bad', task='req-x', parent=None))
        self.assertEqual(self.native.writes('comments', 'add'), [])

    def test_foreign_revision_comment_fails_closed(self):
        record = dict(id='req-other', title='t', description='d', revision=1,
                      acceptance_state='draft', key='R9')
        record['sha256'] = content_hash(record)
        from export_requirements import revision_comment
        self.native.seed('req-x', labels=['requirement'],
                         comments=[dict(id='foreign', text=revision_comment(record),
                                        author='other', created_at='2026-09-25T00:00:00Z')])
        with self.assertRaisesRegex(ValueError, 'belongs to another record'):
            self.apply(self.draft(operation_id='op-foreign', task='req-x', parent=None))

    # --- burned operation IDs ------------------------------------------

    def test_bad_parent_reserves_nothing_and_corrected_retry_succeeds(self):
        self.native.seed('job-1')
        with self.assertRaisesRegex(ValueError, 'parent not found'):
            self.apply(self.draft(parent='pp-nope'))
        self.assertEqual(self.native.writes(), [])
        self.assertEqual(self.receipts(), [])
        result = self.apply(self.draft(parent='job-1'))
        self.assertEqual(result['id'], 'req-1')
        self.assertEqual(len(self.native.writes('create')), 1)

    def test_refused_revise_reserves_nothing_and_corrected_retry_succeeds(self):
        self.apply(self.draft())
        with self.assertRaisesRegex(ValueError, 'must write revision 2'):
            self.apply(self.revise(operation_id='op-badrev', revision=99))
        self.assertEqual(len(self.receipts()), 1)  # only the completed op-1
        result = self.apply(self.revise(operation_id='op-badrev', revision=2))
        self.assertEqual(result['revision'], 2)

    def test_uncertain_create_leaves_pending_that_reconcile_completes(self):
        self.native.seed('job-1')
        self.native.create_outcome = 'lost-response'
        with self.assertRaises(RuntimeError):
            self.apply(self.draft())
        receipts = self.receipts()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0].read_text())['status'], 'pending')
        result = rr.reconcile(self.project, 'op-1', 'operator', 'confirmed native record',
                              'complete', self.native, issue_id='req-1')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(result['issue']['id'], 'req-1')
        self.assertEqual(json.loads(receipts[0].read_text())['status'], 'complete')

    def test_uncertain_create_without_record_reconcile_releases_and_retry_succeeds(self):
        self.native.seed('job-1')
        self.native.create_outcome = 'fail-after-preflight'
        with self.assertRaisesRegex(ValueError, 'outcome is uncertain'):
            self.apply(self.draft())
        self.assertEqual([row['id'] for row in self.native.rows], ['job-1'])
        result = rr.reconcile(self.project, 'op-1', 'operator', 'no record created',
                              'released', self.native)
        self.assertEqual(result['status'], 'released')
        self.native.create_outcome = 'ok'
        result = self.apply(self.draft())
        self.assertEqual(result['id'], 'req-1')

    def test_reconcile_is_idempotent(self):
        self.native.seed('job-1')
        self.native.create_outcome = 'fail-after-preflight'
        with self.assertRaises(ValueError):
            self.apply(self.draft())
        first = rr.reconcile(self.project, 'op-1', 'operator', 'no record', 'released',
                             self.native)
        second = rr.reconcile(self.project, 'op-1', 'operator', 'no record', 'released',
                              self.native)
        self.assertTrue(first['reconciled'])
        self.assertTrue(second['already'])

    def test_requirement_journal_symlink_is_refused(self):
        target = self.project / 'real-journal'
        target.mkdir()
        link = self.project / '.requirement-requests'
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('symlink creation not permitted in this environment')
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.apply(self.draft())
        self.assertEqual(self.native.calls, [])

    # --- operator backfill ---------------------------------------------

    def backfill(self, **extra):
        payload = dict(schema_version=1, operation_id='bf-1', records=[
            dict(task='req-1', kind='requirement', acceptance_state='draft'),
            dict(task='req-2', kind='brd-section', acceptance_state='accepted',
                 evidence='review-2')])
        payload.update(extra)
        return payload

    def test_backfill_applies_only_controlled_labels_and_is_idempotent(self):
        self.native.seed('req-1')
        self.native.seed('req-2')
        result = rr.backfill(self.backfill(), 'operator', self.native, self.project)
        self.assertTrue(result['changed'])
        self.assertFalse(result['reconciled'])
        self.assertEqual(set(self.native.row('req-1')['labels']), {'requirement', 'requirement:draft'})
        self.assertEqual(set(self.native.row('req-2')['labels']), {'brd-section', 'requirement:accepted'})
        # backfill never posts a revision comment
        self.assertEqual(self.native.count('comments', 'add'), 0)
        updates = len(self.native.writes('update'))
        retry = rr.backfill(self.backfill(), 'operator', self.native, self.project)
        self.assertFalse(retry['changed'])
        self.assertTrue(retry['reconciled'])
        self.assertEqual(len(self.native.writes('update')), updates)

    def test_backfill_refuses_unknown_records_and_arbitrary_labels_before_writing(self):
        self.native.seed('req-1')
        with self.assertRaisesRegex(ValueError, 'Unknown requirement record'):
            rr.backfill(self.backfill(operation_id='bf-unknown', records=[
                dict(task='req-1', kind='requirement', acceptance_state='draft'),
                dict(task='missing', kind='requirement', acceptance_state='draft')]),
                'operator', self.native, self.project)
        self.assertEqual(self.native.writes('update'), [])
        with self.assertRaisesRegex(ValueError, 'arbitrary label'):
            rr.backfill(self.backfill(operation_id='bf-bad', records=[
                dict(task='req-1', kind='requirement', acceptance_state='draft', labels=['x'])]),
                'operator', self.native, self.project)
        with self.assertRaisesRegex(ValueError, 'repeats record'):
            rr.backfill(self.backfill(operation_id='bf-dup', records=[
                dict(task='req-1', kind='requirement', acceptance_state='accepted',
                     evidence='e1'),
                dict(task='req-1', kind='requirement', acceptance_state='accepted',
                     evidence='e2')]),
                'operator', self.native, self.project)
        self.assertEqual(self.native.writes('update'), [])

    def test_backfill_accepted_requires_evidence(self):
        self.native.seed('req-1')
        before = list(self.native.writes())
        with self.assertRaisesRegex(ValueError, 'evidence field'):
            rr.backfill(self.backfill(operation_id='bf-noev', records=[
                dict(task='req-1', kind='requirement', acceptance_state='accepted')]),
                'operator', self.native, self.project)
        self.assertEqual(self.native.writes(), before)
        # evidence on a draft entry is a caller error too
        with self.assertRaisesRegex(ValueError, 'applies only to an accepted record'):
            rr.backfill(self.backfill(operation_id='bf-draftev', records=[
                dict(task='req-1', kind='requirement', acceptance_state='draft',
                     evidence='e')]),
                'operator', self.native, self.project)
        self.assertEqual(self.native.writes(), before)

    # --- CLI and routing ------------------------------------------------

    def test_cli_payload_builder_uses_the_subcommand_operation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'payload.json'
            path.write_text(json.dumps({'title': 'R01'}), encoding='utf-8')
            self.assertEqual(rr._cli_payload(path, 'revise')['operation'], 'revise')
            path.write_text(json.dumps({'operation': 'draft'}), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'must not set operation'):
                rr._cli_payload(path, 'draft')

    def test_client_routes_the_requirement_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / 'client.json'
            config.write_text(json.dumps({'transport': 'ssh', 'host': 'h',
                                          'endpoint': '/e.py', 'root': '/r'}), encoding='utf-8')
            captured = {}

            def fake_request(client_config, project, actor, args, action='bd', path=None):
                captured.update(action=action, args=args, project=project, actor=actor)
                return {'stdout': '{}', 'stderr': '', 'returncode': 0}

            argv = ['client.py', '--config', str(config), '--project', 'p', '--actor', 'alice',
                    '--', 'requirement', 'draft', '--file', 'record.json']
            with patch.object(client, 'request', side_effect=fake_request), \
                    patch.object(sys, 'argv', argv):
                self.assertEqual(client.main(), 0)
            self.assertEqual(captured['action'], 'requirement')
            self.assertEqual(captured['args'], ['draft', '--file', 'record.json'])

    def test_endpoint_dispatches_the_requirement_action_as_a_contributor(self):
        if 'fcntl' not in sys.modules:
            stub = types.ModuleType('fcntl')
            stub.LOCK_EX = 1
            stub.flock = lambda *a, **k: None
            sys.modules['fcntl'] = stub
        import endpoint
        seen = {}

        def fake_apply(payload, actor, run, project, operator=False):
            seen.update(payload=payload, actor=actor, project=str(project), operator=operator)
            return {'id': 'req-1'}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            project = root / 'projects' / 'p'
            (project / '.beads').mkdir(parents=True)
            (project / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')
            payload = self.draft()
            with patch.object(endpoint, 'project_dir', return_value=project), \
                    patch.object(endpoint, 'environment', return_value={}), \
                    patch.object(rr, 'apply_native', side_effect=fake_apply):
                answer = endpoint.execute(root, {'project': 'p', 'actor': 'alice',
                                                 'action': 'requirement',
                                                 'args': [json.dumps(payload)]})
        self.assertEqual(answer['returncode'], 0)
        self.assertEqual(json.loads(answer['stdout']), {'id': 'req-1'})
        self.assertEqual(seen['payload']['operation'], 'draft')
        self.assertEqual(seen['actor'], 'alice')
        self.assertFalse(seen['operator'])


if __name__ == '__main__':
    unittest.main()
