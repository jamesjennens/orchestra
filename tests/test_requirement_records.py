import copy
import json
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
    """Stateful native seam: exported rows plus create/comment/update writes."""

    def __init__(self):
        self.calls = []
        self.rows = []
        self.counter = 0
        self.actor = 'alice'
        self.create_outcome = 'ok'
        self.comment_outcome = 'ok'
        self.update_outcome = 'ok'

    def seed(self, task, title='Seeded record', description='Seeded body', labels=None, comments=None):
        row = dict(id=task, title=title, description=description, issue_type='task',
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
            if self.create_outcome == 'not-written':
                raise RuntimeError('create not written')
            self.counter += 1
            task = 'req-%d' % self.counter
            labels = args[args.index('--labels') + 1].split(',') if '--labels' in args else []
            self.rows.append(dict(id=task, title=args[args.index('--title') + 1],
                                  description=args[args.index('--description') + 1],
                                  issue_type='task', status='open', labels=labels, comments=[]))
            if self.create_outcome == 'lost-response':
                raise RuntimeError('create committed, response lost')
            return json.dumps({'id': task})
        if args[0] == 'comments':
            if args[1] == 'add':
                task, body = args[2], args[3]
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

    def apply(self, payload, actor='alice'):
        return rr.apply_native(copy.deepcopy(payload), actor, self.native, self.project)

    def _without_none(self, payload):
        for name in ('task', 'parent', 'key', 'revision', 'title', 'description'):
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
                       acceptance_state='accepted')
        payload.update(extra)
        return self._without_none(payload)

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
        create = next(call for call in self.native.calls if call[0] == 'create')
        self.assertIn('--no-inherit-labels', create)
        self.assertEqual(create[create.index('--parent') + 1], 'job-1')
        self.assertEqual(create[create.index('--type') + 1], 'task')

    def test_draft_retry_is_idempotent(self):
        first = self.apply(self.draft())
        second = self.apply(self.draft())
        self.assertEqual(second['id'], first['id'])
        self.assertTrue(second['reconciled'])
        self.assertFalse(second['created'])
        self.assertEqual(self.native.count('create'), 1)
        self.assertEqual(len(self.native.comments('req-1')), 1)

    def test_draft_selects_existing_record_without_creating(self):
        self.native.seed('req-x')
        result = self.apply(self.draft(operation_id='op-select', task='req-x', parent=None))
        self.assertEqual(result['id'], 'req-x')
        self.assertFalse(result['created'])
        self.assertEqual(self.native.count('create'), 0)
        labels = set(self.native.row('req-x')['labels'])
        self.assertLessEqual({'requirement', 'requirement:draft'}, labels)
        self.assertEqual(self.native.record('req-x', 1)['id'], 'req-x')

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
        self.assertEqual(self.native.count('create'), 0)

    # --- revise --------------------------------------------------------

    def test_revise_posts_next_revision_and_switches_state_label(self):
        self.apply(self.draft())
        result = self.apply(self.revise())
        self.assertEqual(result['revision'], 2)
        labels = set(self.native.row('req-1')['labels'])
        self.assertLessEqual({'requirement', 'requirement:accepted'}, labels)
        self.assertNotIn('requirement:draft', labels)
        self.assertEqual(len(self.native.comments('req-1')), 2)
        self.assertEqual(self.native.record('req-1', 2)['acceptance_state'], 'accepted')
        self.assertEqual(self.native.count('create'), 1)

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
        self.native.seed('req-x')
        with self.assertRaisesRegex(ValueError, 'use draft first'):
            self.apply(self.revise(operation_id='op-bare', task='req-x', revision=1))
        self.assertEqual(self.native.comments('req-x'), [])

    def test_revise_requires_task_and_revision(self):
        with self.assertRaisesRegex(ValueError, 'needs the existing requirement task id'):
            self.apply(self.revise(operation_id='op-no-task', task=None))
        with self.assertRaisesRegex(ValueError, 'needs the next revision number'):
            self.apply(self.revise(operation_id='op-no-rev', revision=None))

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
        self.apply(self.draft(operation_id='op-flip', task='req-x', parent=None))
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
        with self.assertRaisesRegex(ValueError, 'different content or actor'):
            self.apply(self.draft(description='A different statement.'))
        self.assertEqual(self.native.count('create'), 1)

    def test_lost_create_response_reconciles_without_duplicate(self):
        self.native.create_outcome = 'lost-response'
        with self.assertRaises(RuntimeError):
            self.apply(self.draft())
        self.native.create_outcome = 'ok'
        result = self.apply(self.draft())
        self.assertTrue(result['reconciled'])
        self.assertEqual(self.native.count('create'), 1)
        self.assertEqual(len(self.native.rows), 1)
        self.assertEqual(len(self.native.comments('req-1')), 1)

    def test_lost_create_without_native_record_fails_closed(self):
        self.native.create_outcome = 'not-written'
        with self.assertRaises(RuntimeError):
            self.apply(self.draft())
        self.native.create_outcome = 'ok'
        with self.assertRaisesRegex(ValueError, 'outcome uncertain'):
            self.apply(self.draft())
        self.assertEqual(self.native.count('create'), 1)
        self.assertEqual(self.native.rows, [])

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
        self.assertEqual(self.native.count('comments', 'add'), 0)

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

    # --- operator backfill ---------------------------------------------

    def backfill(self, **extra):
        payload = dict(schema_version=1, operation_id='bf-1', records=[
            dict(task='req-1', kind='requirement', acceptance_state='draft'),
            dict(task='req-2', kind='brd-section', acceptance_state='accepted')])
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
        updates = self.native.count('update')
        retry = rr.backfill(self.backfill(), 'operator', self.native, self.project)
        self.assertFalse(retry['changed'])
        self.assertTrue(retry['reconciled'])
        self.assertEqual(self.native.count('update'), updates)

    def test_backfill_refuses_unknown_records_and_arbitrary_labels_before_writing(self):
        self.native.seed('req-1')
        with self.assertRaisesRegex(ValueError, 'Unknown requirement record'):
            rr.backfill(self.backfill(operation_id='bf-unknown', records=[
                dict(task='req-1', kind='requirement', acceptance_state='draft'),
                dict(task='missing', kind='requirement', acceptance_state='draft')]),
                'operator', self.native, self.project)
        self.assertEqual(self.native.count('update'), 0)
        with self.assertRaisesRegex(ValueError, 'arbitrary label'):
            rr.backfill(self.backfill(operation_id='bf-bad', records=[
                dict(task='req-1', kind='requirement', acceptance_state='draft', labels=['x'])]),
                'operator', self.native, self.project)
        with self.assertRaisesRegex(ValueError, 'repeats record'):
            rr.backfill(self.backfill(operation_id='bf-dup', records=[
                dict(task='req-1', kind='requirement', acceptance_state='draft'),
                dict(task='req-1', kind='requirement', acceptance_state='accepted')]),
                'operator', self.native, self.project)
        self.assertEqual(self.native.count('update'), 0)

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

    def test_endpoint_dispatches_the_requirement_action(self):
        if 'fcntl' not in sys.modules:
            stub = types.ModuleType('fcntl')
            stub.LOCK_EX = 1
            stub.flock = lambda *a, **k: None
            sys.modules['fcntl'] = stub
        import endpoint
        seen = {}

        def fake_apply(payload, actor, run, project):
            seen.update(payload=payload, actor=actor, project=str(project))
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


if __name__ == '__main__':
    unittest.main()
