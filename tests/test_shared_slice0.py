"""Shared slice 0 (kittrial-5bb.64): the tolerant reader for the accepted designs.

docs/REFERENCE_CATALOG_DESIGN.md (.41), docs/REQUIREMENTS_GATHERING_DESIGN.md (.58)
and docs/CAPABILITY_INDEX_DESIGN.md (.60) each ship a tolerant-reader step before any
writer, so a rollback from a later slice lands on a kit that still reserves their
names, round-trips their journals and hides their records. These tests pin that
contract: the guard for every new prefix and label, the journal schemas in both
directions, every surface of the shared ten-surface list, the `proposals` scope,
the deployment `verifiers` key, a rollback round trip and the absence of any writer.
"""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import admin
import agent_prompts
import http_authority
import http_service
import reserved_comments as rc
import work
from render import render

PREFIX_WRITERS = {
    'Kind: reference-entry-v1\n': 'ref propose|revise',
    'Kind: reference-acceptance-v1\n': 'admin.py reference-apply',
    'Kind: requirement-proposal-v1\n': 'proposal submit|revise',
    'Kind: proposal-disposition-v1\n': 'proposal review|decide',
    'Kind: contribution-settings-v1\n': 'proposal settings|hide-self',
    'Kind: capability-entry-v1\n': 'capability propose|revise',
    'Kind: capability-acceptance-v1\n': 'admin.py capability-apply',
    'Kind: capability-verification-v1\n': 'capability check --record',
    'Kind: capability-alias-v1\n': 'capability propose-alias|alias-reject',
}
RESERVED_LABELS = ['reference', 'reference:accepted', 'reference-key:calendar-trading',
                   'proposal', 'proposal:incorporated', 'proposal-key:p-0123456789ab',
                   'contribution-settings', 'capability', 'capability:accepted',
                   'capability-key:review-structured-contribution']
ORDINARY_LABELS = ['references', 'referenced:x', 'proposals', 'proposal-keys',
                   'contribution-settings-old', 'contributions', 'capabilities',
                   'capability-keys', 'Capability', 'ops']
JOURNALS = ('.reference-requests', '.proposal-requests', '.capability-requests')


def receipt(**extra):
    """A receipt as the later slices write it: the frozen minimum plus their keys."""
    record = {'sha256': 'c' * 64, 'status': 'complete', 'actor': 'operator',
              'id': 'kittrial-9.1', 'revision': 2}
    record.update(extra)
    return record


SLICE1_RECEIPTS = {
    '.reference-requests': receipt(
        acceptance={'owners': ['person:james'], 'approvers': ['person:james'],
                    'policy': 'any-owner', 'decision_id': 'dec-1', 'evidence': 'ev-1',
                    'record_sha256': 'd' * 64}),
    '.proposal-requests': receipt(operation='submit'),
    '.capability-requests': receipt(operation='capability-apply', operation_id='batch-1',
                                    key='review.structured-contribution'),
}


def rows_as_a_later_slice_writes_them():
    """One ordinary task plus the anchors .41/.58/.60 create, with v1 and an unknown v2."""
    entry = {'kind': 'reference'}
    return [
        {'id': 'kit-1', 'title': 'Ordinary task', 'status': 'open', 'issue_type': 'task',
         'labels': ['ops'], 'comments': [
             {'id': 1, 'text': 'A normal journal entry.', 'created_at': '2026-10-01T10:00:00Z'},
             # Forged on an older kit before the prefix was reserved: still hidden.
             {'id': 2, 'text': 'Kind: capability-alias-v1\n{"alias":"steer"}',
              'created_at': '2026-10-01T10:01:00Z'},
             {'id': 3, 'text': 'Kind: reference-entry-v2\n{}', 'created_at': '2026-10-01T10:02:00Z'}]},
        {'id': 'kit-2', 'title': 'calendar.trading', 'status': 'closed', 'issue_type': 'task',
         'labels': ['reference', 'reference:accepted', 'reference-key:calendar-trading'],
         'comments': [{'id': 4, 'text': 'Kind: reference-entry-v1\n' + json.dumps(entry),
                       'created_at': '2026-10-01T11:00:00Z'}]},
        # Status is irrelevant: an open anchor is hidden too.
        {'id': 'kit-3', 'title': 'p-0123456789ab', 'status': 'open', 'issue_type': 'task',
         'assignee': 'session-x', 'labels': ['proposal', 'proposal:submitted'],
         'comments': [{'id': 5, 'text': 'Kind: requirement-proposal-v1\n{}',
                       'created_at': '2026-10-01T12:00:00Z'}]},
        {'id': 'kit-4', 'title': 'contribution settings', 'status': 'closed',
         'issue_type': 'task', 'labels': ['contribution-settings'],
         'comments': [{'id': 6, 'text': 'Kind: contribution-settings-v1\n{}',
                       'created_at': '2026-10-01T13:00:00Z'}]},
        {'id': 'kit-5', 'title': 'review.structured-contribution', 'status': 'closed',
         'issue_type': 'task', 'labels': ['capability', 'capability:draft'],
         'comments': [{'id': 7, 'text': 'Kind: capability-entry-v1\n{}',
                       'created_at': '2026-10-01T14:00:00Z'},
                      {'id': 8, 'text': 'Kind: capability-entry-v9\n{"future": true}',
                       'created_at': '2026-10-01T14:01:00Z'}]},
    ]


ANCHOR_IDS = {'kit-2', 'kit-3', 'kit-4', 'kit-5'}
RECORD_MARKERS = ('Kind: reference-', 'Kind: requirement-proposal-', 'Kind: proposal-disposition-',
                  'Kind: contribution-settings-', 'Kind: capability-')


class GuardTests(unittest.TestCase):
    def test_every_v1_prefix_is_reserved_and_names_its_writer(self):
        for prefix, writer in PREFIX_WRITERS.items():
            with self.subTest(prefix=prefix):
                body = prefix + '{}'
                self.assertIn(writer, rc.reserved_match(body)[2])
                for args, attachments in (
                        (['comments', 'add', 'kit-1', body], {}),
                        (['comments', '--json', 'add', 'kit-1', body], {}),
                        (['comments', 'add', 'kit-1', '@attachment:0'],
                         {'0': {'flag': '--file', 'text': body}})):
                    with self.assertRaisesRegex(ValueError, 'Refusing raw'):
                        rc.check_raw_request(args, attachments, actor='alice', task='kit-1')
                for lookalike in ('﻿' + body, body.replace('\n', '\r\n')):
                    with self.assertRaises(ValueError):
                        rc.check_comment_body(lookalike, 'positional', actor='alice', task='kit-1')

    def test_prose_and_unreserved_versions_are_not_refused(self):
        rc.check_comment_body('We discussed Kind: capability-entry-v1 today.', 'positional',
                              actor='alice', task='kit-1')
        # Only v1 is reserved (.58 3.8); a newer version is still a record comment, hidden
        # from every surface, and classified unsupported for the later readers.
        rc.check_comment_body('Kind: capability-entry-v2\n{}', 'positional', actor='alice', task='kit-1')
        self.assertTrue(rc.is_record_comment('Kind: capability-entry-v2\n{}'))
        self.assertEqual(rc.record_comment_kind('Kind: capability-entry-v2\n{}'),
                         ('capability-entry', 2, 'unsupported'))
        self.assertEqual(rc.record_comment_kind('Kind: reference-entry-v1\n{}'),
                         ('reference-entry', 1, 'reserved'))
        self.assertIsNone(rc.record_comment_kind('Kind: requirement-revision-v1\n{}'))

    def test_reserved_labels_and_their_neighbours(self):
        for label in RESERVED_LABELS:
            self.assertEqual(rc.reserved_label(label), label, label)
        for label in ORDINARY_LABELS:
            self.assertIsNone(rc.reserved_label(label), label)

    def test_every_label_writing_spelling_is_refused(self):
        for label in RESERVED_LABELS:
            for args in (['update', 'kit-1', '--add-label', label],
                         ['update', 'kit-1', '--set-labels', 'ops,' + label],
                         ['update', 'kit-1', '--remove-label', label],
                         ['create', '--title', 't', '--labels', label],
                         ['create', '--title', 't', '--label', label],
                         ['create', '--title', 't', '-l', label]):
                with self.subTest(args=args):
                    self.assertEqual(rc.reserved_label_in_args(args), label)

    def test_read_before_write_guard_sees_inherited_and_held_labels(self):
        for label in RESERVED_LABELS:
            self.assertEqual(rc.first_reserved_label(['ops', label]), label)
        self.assertIsNone(rc.first_reserved_label(ORDINARY_LABELS))


class JournalTests(unittest.TestCase):
    """Whitelisted, backed up, restored and validated in both directions."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.source = self.root / 'projects' / 'source'
        self.destination = self.root / 'projects' / 'destination'
        self.source.mkdir(parents=True)
        self.destination.mkdir()
        (self.root / 'backups' / 'source').mkdir(parents=True)
        self.bundle = self.root / 'backups' / 'source.coordination.json'
        patcher = patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=Mock(), LOCK_EX=2)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def name(self, journal, char='a'):
        return '%s/%s.json' % (journal, char * 64)

    def write(self, journal, record, char='a'):
        path = self.source / self.name(journal, char)
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(record), encoding='utf-8')

    def test_a_later_slice_journal_round_trips_through_backup_and_restore(self):
        for journal, record in SLICE1_RECEIPTS.items():
            self.write(journal, record)
        with patch.object(admin, 'run_bd', return_value='synced'):
            admin.backup_project(self.root, 'source')
        files = json.loads(self.bundle.read_text(encoding='utf-8'))['files']
        for journal, record in SLICE1_RECEIPTS.items():
            self.assertEqual(files[self.name(journal)], record)
        admin.restore_coordination(self.root, 'source', 'destination')
        for journal, record in SLICE1_RECEIPTS.items():
            restored = json.loads((self.destination / self.name(journal)).read_text(encoding='utf-8'))
            self.assertEqual(restored, record)

    def test_malformed_receipts_are_refused_on_backup_validation_and_restore(self):
        bad = [receipt(sha256='nothex'), receipt(status='done'), receipt(revision=0),
               receipt(revision=True), receipt(actor=''), receipt(id=' '),
               {'status': 'complete'}, []]
        for journal in JOURNALS:
            for record in bad:
                with self.subTest(journal=journal, record=record):
                    with self.assertRaises(ValueError):
                        admin.validate_coordination_files({self.name(journal): record})
                    with patch.object(admin, 'coordination_backup',
                                      return_value={self.name(journal): record}):
                        with self.assertRaises(ValueError):
                            admin.restore_coordination(self.root, 'source', 'destination')
                    self.assertEqual(list(self.destination.iterdir()), [])
        for operation in (5, '', '  ', None):
            with self.assertRaisesRegex(ValueError, 'proposal receipt operation'):
                admin.validate_coordination_files(
                    {self.name('.proposal-requests'): receipt(operation=operation)})

    def test_a_malformed_receipt_stops_the_backup_before_native_sync(self):
        self.write('.capability-requests', {'status': 'complete'})
        with patch.object(admin, 'run_bd') as native:
            with self.assertRaises(ValueError):
                admin.backup_project(self.root, 'source')
        native.assert_not_called()

    def test_paths_must_stay_in_a_known_journal(self):
        for name in ('.capability-requests/../../escape.json',
                     '.proposal-requests/notahash.json',
                     '.reference-requests/' + 'b' * 63 + '.json',
                     '.alias-requests/' + 'b' * 64 + '.json'):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, 'Invalid coordination backup path'):
                    admin.validate_coordination_files({name: receipt()})

    def test_a_symlinked_record_journal_is_refused(self):
        outside = self.root / 'outside'
        outside.mkdir()
        try:
            os.symlink(outside, self.source / '.capability-requests', target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest('cannot create symlinks here')
        with patch.object(admin, 'run_bd'):
            with self.assertRaisesRegex(ValueError, 'must not be a symlink'):
                admin.backup_project(self.root, 'source')


class SurfaceTests(unittest.TestCase):
    """Every surface of the shared ten-surface list hides record anchors and comments."""

    def setUp(self):
        self.rows = rows_as_a_later_slice_writes_them()

    def test_work_and_work_mine_never_list_an_anchor(self):
        listed = work.queue(self.rows, 'session-x', [])
        self.assertEqual([item['task'] for item in listed['items']], ['kit-1'])
        mine = work.queue(self.rows, 'session-x', ['--mine'])
        self.assertEqual(mine['items'], [])

    def test_render_hides_anchors_and_record_comments_from_every_view(self):
        with tempfile.TemporaryDirectory() as temp:
            views = Path(temp) / 'views'
            render(self.rows, views)
            texts = {path.relative_to(views).as_posix(): path.read_text(encoding='utf-8')
                     for path in views.rglob('*') if path.is_file()}
            issues = [json.loads(line) for line in texts['issues.jsonl'].splitlines()]
            self.assertEqual([row['id'] for row in issues], ['kit-1'])
            self.assertEqual([c['id'] for c in issues[0]['comments']], [1])
            for name, text in texts.items():
                for anchor in ANCHOR_IDS:
                    self.assertNotIn(anchor, text, name)
                for marker in RECORD_MARKERS:
                    self.assertNotIn(marker, text, name)
            # Rendering is a projection: no record journal appears beside the views.
            self.assertFalse(any((Path(temp) / journal).exists() for journal in JOURNALS))

    def test_hide_records_copies_rows_and_never_mutates_the_input(self):
        before = json.dumps(self.rows, sort_keys=True)
        shown = rc.hide_records(self.rows)
        self.assertEqual(json.dumps(self.rows, sort_keys=True), before)
        self.assertEqual([row['id'] for row in shown], ['kit-1'])

    def test_agent_prompts_skip_a_labelled_anchor(self):
        items = [dict(row, review_state='awaiting-review') for row in self.rows]
        out = agent_prompts.classify({'id': 'p', 'name': 'P'}, {agent_prompts.CAP_APPROVE},
                                     items, 'someone-else', set(), None)
        listed = {item['id'] for group in out['classes'].values()
                  for item in group if isinstance(item, dict)}
        self.assertEqual(listed & ANCHOR_IDS, set())
        self.assertIn('kit-1', listed)

    def test_the_endpoint_backend_read_seam_drops_anchors(self):
        fake = types.SimpleNamespace(actor_namespace='http',
                                     _run=lambda *args: self.rows,
                                     _in_project=lambda rows, project: rows)
        snapshot = http_service.EndpointBackend.read_tasks(fake, 'kittrial')
        self.assertEqual([row['id'] for row in snapshot['items']], ['kit-1'])
        self.assertEqual(snapshot['total'], 1)


try:
    from test_http_service import ServerHarness
except Exception:  # pragma: no cover - harness import problems surface in its own module
    ServerHarness = None


@unittest.skipIf(ServerHarness is None, 'HTTP harness unavailable')
class HttpSurfaceTests(ServerHarness if ServerHarness else unittest.TestCase):
    def test_http_task_routes_hide_and_refuse_record_anchors(self):
        token = self.admin_token()
        project = self.create_project(token, 'slice-zero')
        ids = []
        for title in ('ordinary', 'reference anchor', 'capability anchor'):
            created = self.create_task(token, project, title)
            self.assertEqual(201, created.status, created.data)
            ids.append(created.data['id'])
        ordinary, reference, capability = ids
        tasks = self.backend.state['tasks']
        tasks[reference]['labels'] = ['reference', 'reference:accepted']
        tasks[reference]['status'] = 'closed'
        tasks[capability]['labels'] = ['capability', 'capability:draft']
        for query in ('', '?status=closed', '?status=active', '?q=anchor'):
            listed = self.request('GET', '/v1/projects/%s/tasks%s' % (project, query), token=token)
            self.assertEqual(200, listed.status, listed.data)
            shown = {item['id'] for item in listed.data['items']}
            self.assertEqual(shown & {reference, capability}, set(), query)
        queue = self.request('GET', '/v1/projects/%s/queue' % project, token=token)
        self.assertEqual({item['id'] for item in queue.data['items']} & {reference, capability}, set())
        for anchor in (reference, capability):
            for suffix in ('', '/brief', '/history'):
                response = self.request('GET', '/v1/projects/%s/tasks/%s%s' % (project, anchor, suffix),
                                        token=token)
                self.assertEqual(404, response.status, (suffix, response.data))
                self.assertNotIn('Kind:', response.body.decode('utf-8', 'replace'))
        for suffix in ('', '/brief', '/history'):
            response = self.request('GET', '/v1/projects/%s/tasks/%s%s' % (project, ordinary, suffix),
                                    token=token)
            self.assertEqual(200, response.status, (suffix, response.data))


class ScopeAndConfigTests(unittest.TestCase):
    def test_the_proposals_scope_is_recognised_and_grants_nothing(self):
        self.assertIn('proposals', http_authority.CREDENTIAL_SCOPES)
        self.assertEqual(http_authority.SCOPE_CAPABILITIES['proposals'], frozenset())

    def test_the_deployment_verifiers_key_is_tolerated_and_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'projects').mkdir()
            marker = root / 'deployment.private.json'
            marker.write_text(json.dumps({'password': 'x', 'operators': ['alice'],
                                          'verifiers': ['ci-host']}), encoding='utf-8')
            self.assertEqual(admin.operators(root), frozenset({'alice'}))

            def cli(*argv):
                with patch.object(sys, 'argv', ['admin.py', '--root', str(root), *argv]), \
                        patch.object(admin, 'root_path', return_value=root), \
                        patch.dict(os.environ, {}, clear=False), \
                        contextlib.redirect_stdout(io.StringIO()):
                    os.environ.pop('ORCHESTRA_OPERATORS', None)
                    admin.main()

            cli('operators', 'add', 'bob')
            cli('operators', 'remove', 'alice', '--confirm-revoke')
            stored = json.loads(marker.read_text(encoding='utf-8'))
            self.assertEqual(stored['operators'], ['bob'])
            self.assertEqual(stored['verifiers'], ['ci-host'])

    def test_a_sidecar_carrying_verifiers_still_restores(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'backups').mkdir()
            sidecar = {'schema_version': 1, 'status': 'complete', 'operators': ['alice'],
                       'verifiers': ['ci-host'],
                       'files': {'.capability-requests/' + 'a' * 64 + '.json':
                                 SLICE1_RECEIPTS['.capability-requests']}}
            (root / 'backups' / 'source.coordination.json').write_text(json.dumps(sidecar),
                                                                        encoding='utf-8')
            with patch.object(admin, 'complete_sidecar', return_value=sidecar):
                files = admin.coordination_backup(root, 'source')
            self.assertEqual(list(files), ['.capability-requests/' + 'a' * 64 + '.json'])


class NoWriterTests(unittest.TestCase):
    """Slice 0 writes nothing: no kit module but the guard names a record prefix, and
    the record journals are only ever read (backup) or validated (restore)."""

    def test_only_the_guard_knows_the_record_prefixes(self):
        offenders = []
        for path in sorted(KIT.glob('*.py')):
            text = path.read_text(encoding='utf-8')
            for marker in RECORD_MARKERS + tuple(prefix.strip() for prefix in PREFIX_WRITERS):
                if marker in text and path.name != 'reserved_comments.py':
                    offenders.append((path.name, marker))
        self.assertEqual(offenders, [])

    def test_record_journals_are_never_created_by_this_kit(self):
        for path in sorted(KIT.glob('*.py')):
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if any(journal in line for journal in JOURNALS) or 'RECORD_JOURNALS' in line:
                    self.assertNotIn('mkdir', line, '%s:%d' % (path.name, number))
                    self.assertNotIn('write_text', line, '%s:%d' % (path.name, number))
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            project = root / 'projects' / 'source'
            project.mkdir(parents=True)
            (root / 'backups' / 'source').mkdir(parents=True)
            with patch.dict(sys.modules, {'fcntl': types.SimpleNamespace(flock=Mock(), LOCK_EX=2)}), \
                    patch.object(admin, 'run_bd', return_value='synced'):
                admin.backup_project(root, 'source')
            self.assertFalse(any((project / journal).exists() for journal in JOURNALS))


if __name__ == '__main__':
    unittest.main()
