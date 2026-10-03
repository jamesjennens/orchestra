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
    'Kind: proposal-disposition-v1\n': 'admin.py proposal-review|proposal-decide',
    'Kind: contribution-settings-v1\n': 'admin.py proposal-settings',
    'Kind: capability-entry-v1\n': 'capability propose|revise',
    'Kind: capability-acceptance-v1\n': 'admin.py capability-apply',
    'Kind: capability-verification-v1\n': 'capability check --record',
    'Kind: capability-alias-v1\n': 'capability propose-alias|alias-reject',
}
RESERVED_LABELS = ['reference:accepted', 'reference-key:calendar-trading',
                   'proposal:incorporated', 'proposal-key:p-0123456789ab',
                   'capability:accepted', 'capability-key:review-structured-contribution']
# The exact record type labels are ordinary labels on an ordinary task (a project may
# use them already: live jjbp-j03.20 carries `proposal`); only a real anchor's are
# protected, by the endpoint guard.
TYPE_LABELS = ['reference', 'proposal', 'contribution-settings', 'capability']
ORDINARY_LABELS = TYPE_LABELS + ['references', 'referenced:x', 'proposals', 'proposal-keys',
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
        # A project's own use of the words (the jjbp-j03.20 case): ordinary tasks.
        {'id': 'kit-6', 'title': 'Suggestion triage', 'status': 'open', 'issue_type': 'task',
         'labels': ['proposal'], 'comments': []},
        {'id': 'kit-7', 'title': 'Capability planning', 'status': 'open', 'issue_type': 'task',
         'labels': ['capability'], 'comments': [
             {'id': 9, 'text': 'Plain prose about capabilities.', 'created_at': '2026-10-01T15:00:00Z'}]},
        # The wrong family is not evidence: a `reference` label needs a reference record.
        {'id': 'kit-8', 'title': 'Mislabelled', 'status': 'open', 'issue_type': 'task',
         'labels': ['reference'], 'comments': [
             {'id': 10, 'text': 'Kind: capability-entry-v1\n{}', 'created_at': '2026-10-01T16:00:00Z'}]},
    ]


ANCHOR_IDS = {'kit-2', 'kit-3', 'kit-4', 'kit-5'}
VISIBLE_IDS = ['kit-1', 'kit-6', 'kit-7', 'kit-8']
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

    def test_every_version_of_each_kind_is_reserved_and_prose_is_not(self):
        rc.check_comment_body('We discussed Kind: capability-entry-v1 today.', 'positional',
                              actor='alice', task='kit-1')
        for prefix in PREFIX_WRITERS:
            for version in ('v2', 'v7', 'v123'):
                body = prefix.replace('v1\n', version + '\n') + '{}'
                with self.subTest(body=body):
                    with self.assertRaisesRegex(ValueError, 'Refusing raw'):
                        rc.check_comment_body(body, 'positional', actor='alice', task='kit-1')
        self.assertTrue(rc.is_record_comment('Kind: capability-entry-v2\n{}'))
        self.assertEqual(rc.record_comment_kind('Kind: capability-entry-v2\n{}'),
                         ('capability-entry', 2, 'unsupported'))
        self.assertEqual(rc.record_comment_kind('Kind: reference-entry-v1\n{}'),
                         ('reference-entry', 1, 'supported'))
        self.assertEqual(rc.record_comment_kind('Kind: capability-gizmo-v1\n{}'),
                         ('capability-gizmo', 1, 'unsupported'))
        self.assertIsNone(rc.record_comment_kind('Kind: requirement-revision-v1\n{}'))

    def test_reserved_labels_and_their_neighbours(self):
        for label in RESERVED_LABELS:
            self.assertEqual(rc.reserved_label(label), label, label)
        for label in ORDINARY_LABELS:
            self.assertIsNone(rc.reserved_label(label), label)

    def test_type_labels_are_writable_on_ordinary_tasks(self):
        for label in TYPE_LABELS:
            for args in (['update', 'kit-6', '--add-label', label],
                         ['update', 'kit-6', '--remove-label', label],
                         ['create', '--title', 't', '--labels', label]):
                self.assertIsNone(rc.reserved_label_in_args(args), args)

    def test_an_anchor_needs_its_label_and_a_v1_record_of_the_same_family(self):
        rows = {row['id']: row for row in rows_as_a_later_slice_writes_them()}
        self.assertEqual({rid for rid, row in rows.items() if rc.is_record_anchor(row)}, ANCHOR_IDS)
        # request:/request-content: are not evidence: create-child writes them too.
        self.assertFalse(rc.is_record_anchor({'labels': ['proposal', 'request:' + 'a' * 64],
                                              'comments': []}))
        # A row read without comments is never assumed to be an anchor.
        self.assertFalse(rc.is_record_anchor({'labels': ['capability']}))
        self.assertTrue(rc.carries_record_label({'labels': ['capability']}))

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
        self.assertEqual(sorted(item['task'] for item in listed['items']), VISIBLE_IDS)
        mine = work.queue(self.rows, 'session-x', ['--mine'])
        self.assertEqual(mine['items'], [])

    def test_render_hides_anchors_and_record_comments_from_every_view(self):
        with tempfile.TemporaryDirectory() as temp:
            views = Path(temp) / 'views'
            render(self.rows, views)
            texts = {path.relative_to(views).as_posix(): path.read_text(encoding='utf-8')
                     for path in views.rglob('*') if path.is_file()}
            issues = [json.loads(line) for line in texts['issues.jsonl'].splitlines()]
            self.assertEqual([row['id'] for row in issues], VISIBLE_IDS)
            self.assertEqual([c['id'] for c in issues[0]['comments']], [1])
            self.assertEqual(issues[3]['comments'], [])
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
        self.assertEqual([row['id'] for row in shown], VISIBLE_IDS)

    def test_refresh_prunes_pages_it_no_longer_renders(self):
        with tempfile.TemporaryDirectory() as temp:
            views = Path(temp) / 'views'
            stale = {'jobs/kit-2.md': 'Kind: reference-entry-v1\n{"stale": true}',
                     'jobs/gone-1.md': 'an issue that no longer exists',
                     'journal/2026-01-01.md': 'Kind: capability-alias-v1\n{}'}
            for name, text in stale.items():
                (views / name).parent.mkdir(parents=True, exist_ok=True)
                (views / name).write_text(text, encoding='utf-8')
            result = render(self.rows, views)
            self.assertEqual(result['pruned'], 3)
            for name in stale:
                self.assertFalse((views / name).exists(), name)
            self.assertEqual(sorted(p.stem for p in (views / 'jobs').glob('*.md')), VISIBLE_IDS)
            self.assertTrue((views / 'journal' / 'INDEX.md').exists())

    def test_refresh_owns_jobs_and_journal_and_removes_hand_made_pages(self):
        with tempfile.TemporaryDirectory() as temp:
            views = Path(temp) / 'views'
            (views / 'jobs').mkdir(parents=True)
            (views / 'jobs' / 'my-notes.md').write_text('hand-made', encoding='utf-8')
            (views / 'jobs' / 'README.txt').write_text('not a page', encoding='utf-8')
            (views / 'notes.md').write_text('top level', encoding='utf-8')
            (views / 'jobs' / 'folder.md').mkdir()
            result = render(self.rows, views)
            self.assertEqual(result['pruned'], 1)
            self.assertNotIn('prune_skipped', result)
            self.assertFalse((views / 'jobs' / 'my-notes.md').exists())
            # Only *.md pages in the generated folders are pruned; a real directory
            # that matches the pattern is left alone and refresh still succeeds.
            self.assertTrue((views / 'jobs' / 'README.txt').exists())
            self.assertTrue((views / 'notes.md').exists())
            self.assertTrue((views / 'jobs' / 'folder.md').is_dir())

    def symlink(self, link, target, directory):
        try:
            os.symlink(target, link, target_is_directory=directory)
        except (OSError, NotImplementedError):
            self.skipTest('cannot create symlinks here')

    def test_refresh_never_prunes_through_a_symlinked_jobs_or_journal_folder(self):
        # kittrial-5bb.72 (e64r2-prune.log): a symlinked jobs/ deleted precious.md
        # in its target, outside views.
        for folder in ('jobs', 'journal'):
            with self.subTest(folder=folder), tempfile.TemporaryDirectory() as temp:
                views = Path(temp) / 'views'
                views.mkdir()
                outside = Path(temp) / 'outside'
                outside.mkdir()
                for name in ('precious.md', '2026-01-01.md', 'gone-1.md'):
                    (outside / name).write_text('keep me', encoding='utf-8')
                self.symlink(views / folder, outside, True)
                result = render(self.rows, views)
                self.assertEqual(result['prune_skipped'], [folder])
                self.assertEqual(result['pruned'], 0)
                for name in ('precious.md', '2026-01-01.md', 'gone-1.md'):
                    self.assertEqual((outside / name).read_text(encoding='utf-8'), 'keep me')

    def test_refresh_never_prunes_a_folder_that_resolves_outside_views(self):
        # A Windows junction needs no symlink privilege, and before Python 3.12
        # Path.is_symlink() is False for it, yet it resolves outside views.
        try:
            import _winapi
            create_junction = _winapi.CreateJunction
        except (ImportError, AttributeError):
            self.skipTest('directory junctions are Windows-only (POSIX is covered by the symlink case)')
        with tempfile.TemporaryDirectory() as temp:
            views = Path(temp) / 'views'
            views.mkdir()
            outside = Path(temp) / 'outside'
            outside.mkdir()
            (outside / 'precious.md').write_text('keep me', encoding='utf-8')
            create_junction(str(outside), str(views / 'jobs'))
            try:
                result = render(self.rows, views)
                self.assertEqual(result['prune_skipped'], ['jobs'])
                self.assertEqual((outside / 'precious.md').read_text(encoding='utf-8'), 'keep me')
            finally:
                os.rmdir(views / 'jobs')  # removes the junction, never its target

    def test_a_symlinked_page_is_removed_as_a_link_and_its_target_kept(self):
        with tempfile.TemporaryDirectory() as temp:
            views = Path(temp) / 'views'
            (views / 'jobs').mkdir(parents=True)
            target = Path(temp) / 'target.md'
            target.write_text('keep me', encoding='utf-8')
            self.symlink(views / 'jobs' / 'gone-1.md', target, False)
            result = render(self.rows, views)
            self.assertEqual(result['pruned'], 1)
            self.assertFalse(os.path.lexists(views / 'jobs' / 'gone-1.md'))
            self.assertEqual(target.read_text(encoding='utf-8'), 'keep me')

    def test_history_brief_and_checkpoint_snapshots_drop_record_comments(self):
        import briefing
        for task in ('kit-1', 'kit-5'):
            data = briefing.snapshot(self.rows, 'kit', task)
            bodies = [entry['body'] for entry in data['entries']]
            self.assertFalse(any(body.startswith(RECORD_MARKERS) for body in bodies), task)
            page = briefing.history_page(data, 'kit', task, limit=20)
            self.assertFalse(any(e['body'].startswith(RECORD_MARKERS) for e in page['entries']))
        self.assertEqual([e['body'] for e in briefing.snapshot(self.rows, 'kit', 'kit-1')['entries']],
                         ['A normal journal entry.'])

    def test_agent_prompts_skip_a_labelled_anchor(self):
        items = [dict(row, review_state='awaiting-review') for row in self.rows]
        out = agent_prompts.classify({'id': 'p', 'name': 'P'}, {agent_prompts.CAP_APPROVE},
                                     items, 'someone-else', set(), None)
        listed = {item['id'] for group in out['classes'].values()
                  for item in group if isinstance(item, dict)}
        self.assertEqual(listed & ANCHOR_IDS, set())
        self.assertIn('kit-1', listed)

    def seam(self, listed, anchors_reply):
        """An EndpointBackend read seam over a fake list read and anchors reply."""
        calls = []

        def endpoint(action, project, actor, argv):
            calls.append((action, list(argv)))
            reply = anchors_reply(argv)
            return reply if isinstance(reply, dict) and 'returncode' in reply else \
                {'returncode': 0, 'stdout': json.dumps(reply), 'stderr': ''}

        fake = types.SimpleNamespace(actor_namespace='http', _endpoint=endpoint,
                                     _checked=http_service.EndpointBackend._checked,
                                     _run=lambda action, project, actor, argv:
                                         calls.append((action, list(argv[:1]))) or listed,
                                     _in_project=lambda rows, project: rows)
        fake._without_record_anchors = lambda project, rows: \
            http_service.EndpointBackend._without_record_anchors(fake, project, rows)
        return fake, calls

    def test_the_endpoint_backend_read_seam_asks_for_anchors_once_per_snapshot(self):
        listed = [{key: value for key, value in row.items() if key != 'comments'} for row in self.rows]
        answer = lambda argv: {'schema_version': 1, 'anchors': rc.record_anchor_ids(
            [row for row in self.rows if row['id'] in argv])}
        fake, calls = self.seam(listed, answer)
        snapshot = http_service.EndpointBackend.read_tasks(fake, 'kittrial')
        self.assertEqual([row['id'] for row in snapshot['items']], VISIBLE_IDS)
        # One anchors read, naming just the labelled rows (few of them: no export).
        labelled = [row['id'] for row in listed if rc.carries_record_label(row)]
        self.assertEqual(calls, [('bd', ['list']), ('anchors', labelled)])
        # A snapshot with no labelled row costs no anchors read at all.
        plain = [row for row in listed if not rc.carries_record_label(row)]
        fake, calls = self.seam(plain, answer)
        self.assertEqual([r['id'] for r in http_service.EndpointBackend.read_tasks(fake, 'kittrial')['items']],
                         ['kit-1'])
        self.assertEqual(calls, [('bd', ['list'])])

    def test_above_the_id_cap_the_anchors_read_is_one_export(self):
        many = [{'id': 'kit-%d' % (100 + index), 'labels': ['proposal'], 'status': 'open'}
                for index in range(rc.ANCHOR_READ_IDS_MAX + 1)]
        for rows, expected in ((many[:-1], [row['id'] for row in many[:-1]]), (many, [])):
            fake, calls = self.seam(rows, lambda argv: {'schema_version': 1, 'anchors': ['kit-100']})
            shown = http_service.EndpointBackend.read_tasks(fake, 'kittrial')['items']
            self.assertEqual(calls[1], ('anchors', expected))
            self.assertEqual(len(shown), len(rows) - 1)

    def test_a_malformed_anchors_answer_fails_the_list_closed(self):
        listed = [{key: value for key, value in row.items() if key != 'comments'} for row in self.rows]
        for answer in ({'anchors': 'kit-2'}, {'anchors': [2]}, ['kit-2'], None,
                       {'returncode': 2, 'stdout': '', 'stderr': 'ValueError: bad\n'},
                       {'returncode': 1, 'stdout': '', 'stderr': 'boom\n'}):
            fake, _ = self.seam(listed, lambda argv, answer=answer: answer)
            with self.assertRaises(http_service.HttpError):
                http_service.EndpointBackend.read_tasks(fake, 'kittrial')

    def test_an_endpoint_older_than_the_service_is_named(self):
        listed = [{key: value for key, value in row.items() if key != 'comments'} for row in self.rows]
        fake, _ = self.seam(listed, lambda argv: {'returncode': 2, 'stdout': '',
                                                   'stderr': 'ValueError: Unknown action\n'})
        with self.assertRaises(http_service.HttpError) as raised:
            http_service.EndpointBackend.read_tasks(fake, 'kittrial')
        self.assertEqual(raised.exception.status, 501)
        self.assertIn('older than this HTTP service', raised.exception.message)

    def test_record_anchor_ids_is_the_surface_predicate(self):
        self.assertEqual(rc.record_anchor_ids(self.rows), sorted(ANCHOR_IDS))


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
        tasks[reference]['comments'] = [{'id': 1, 'text': 'Kind: reference-entry-v1\n{}'}]
        tasks[capability]['labels'] = ['capability', 'capability:draft']
        tasks[capability]['comments'] = [{'id': 2, 'text': 'Kind: capability-entry-v1\n{}'}]
        # The project's own `proposal` label on an ordinary task stays visible.
        tasks[ordinary]['labels'] = ['proposal']
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
        listed = self.request('GET', '/v1/projects/%s/tasks' % project, token=token)
        self.assertIn(ordinary, {item['id'] for item in listed.data['items']})
        for method, suffix, body in (('PATCH', '', {'title': 'renamed'}),
                                     ('POST', '/claim', {}),
                                     ('POST', '/checkpoints', {'summary': 'x'}),
                                     ('POST', '/reviews', {'operation': 'contribute'})):
            response = self.request(method, '/v1/projects/%s/tasks/%s%s' % (project, capability, suffix),
                                    body, token=token, key='slice0-anchor-write-%s-%s' % (method, suffix.strip('/') or 'task'))
            self.assertEqual(404, response.status, (method, suffix, response.data))
        self.assertEqual(tasks[capability]['title'], 'capability anchor')


try:
    import endpoint
except ImportError:  # endpoint imports fcntl (POSIX-only)
    endpoint = None


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointLabelGuardTests(unittest.TestCase):
    """A real anchor's labels cannot be replaced or removed; an ordinary task's can."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='slice0-guard-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'bin').mkdir()
        (self.root / 'bin' / 'bd').write_text('', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            '{"password": "x", "unit": "none", "port": "1"}', encoding='utf-8')
        self.path = self.root / 'projects' / 'pp'
        self.path.mkdir(parents=True)

    def guard(self, row, args):
        def run(argv, **kwargs):
            payload = row['comments'] if 'comments' in argv else [
                {key: value for key, value in row.items() if key != 'comments'}]
            return types.SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr='')
        with patch.object(endpoint.subprocess, 'run', run):
            endpoint._guard_reserved_labels(self.root, self.path, args, 'worker')

    def test_replacing_or_removing_labels_on_a_real_anchor_is_refused(self):
        anchor = {'id': 'pp-1', 'labels': ['contribution-settings'],
                  'comments': [{'id': 1, 'text': 'Kind: contribution-settings-v1\n{}'}]}
        for args in (['update', 'pp-1', '--set-labels', 'ops'],
                     ['update', 'pp-1', '--remove-label', 'contribution-settings']):
            with self.assertRaisesRegex(ValueError, 'record anchor'):
                self.guard(anchor, args)

    def test_an_ordinary_task_with_a_type_label_stays_editable(self):
        ordinary = {'id': 'pp-2', 'labels': ['proposal'], 'comments': []}
        for args in (['update', 'pp-2', '--set-labels', 'ops'],
                     ['update', 'pp-2', '--remove-label', 'proposal']):
            self.guard(ordinary, args)


@unittest.skipIf(endpoint is None, 'endpoint imports fcntl (POSIX-only)')
class EndpointAnchorsActionTests(unittest.TestCase):
    """kittrial-5bb.71: the read-only anchors action answers a snapshot from one export."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='slice0-anchors-')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'bin').mkdir()
        (self.root / 'bin' / 'bd').write_text('', encoding='utf-8')
        (self.root / 'deployment.private.json').write_text(
            '{"password": "x", "unit": "none", "port": "1"}', encoding='utf-8')
        path = self.root / 'projects' / 'pp'
        (path / '.beads').mkdir(parents=True)
        (path / '.beads' / 'metadata.json').write_text('{}', encoding='utf-8')

    def execute(self, rows, args=None, show=None, **request_extra):
        calls = []

        def run(argv, **kwargs):
            command = list(argv[argv.index('--sandbox') + 1:])
            calls.append(command)
            if 'show' in command:
                return show(command) if show else types.SimpleNamespace(
                    returncode=0, stderr='',
                    stdout=json.dumps([row for row in rows if row['id'] in command]))
            text = ''.join(json.dumps(row) + '\n' for row in rows)
            return types.SimpleNamespace(returncode=0, stdout=text, stderr='')
        request = dict({'project': 'pp', 'actor': 'http/read', 'action': 'anchors'}, **request_extra)
        if args is not None:
            request['args'] = args
        with patch.object(endpoint.native.subprocess, 'run', run), \
                patch.object(endpoint.fcntl, 'flock', side_effect=AssertionError('no lock')), \
                patch.object(endpoint, 'run_guarded', side_effect=AssertionError('no journal')):
            reply = endpoint.execute(self.root, request)
        return reply, calls

    def test_one_export_gives_the_anchor_ids_by_the_surface_predicate(self):
        rows = rows_as_a_later_slice_writes_them()
        reply, calls = self.execute(rows, operation_id='ssh-op-0001')
        self.assertEqual(reply['returncode'], 0, reply)
        self.assertEqual(json.loads(reply['stdout']),
                         {'schema_version': 1, 'anchors': rc.record_anchor_ids(rows)})
        self.assertEqual(rc.record_anchor_ids(rows), sorted(ANCHOR_IDS))
        self.assertEqual([call[-2:] for call in calls], [['export', '--all']])

    def test_named_rows_are_read_in_one_show_with_comments(self):
        rows = rows_as_a_later_slice_writes_them()
        labelled = [row['id'] for row in rows if rc.carries_record_label(row)]
        reply, calls = self.execute(rows, args=labelled)
        self.assertEqual(json.loads(reply['stdout'])['anchors'], sorted(ANCHOR_IDS))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][-(len(labelled) + 3):], ['show', *labelled, '--json', '--include-comments'])

    def test_rows_deleted_after_the_list_are_simply_not_anchors(self):
        gone = lambda command: types.SimpleNamespace(
            returncode=1, stderr='Error fetching pp-9: no issue found matching "pp-9"\n',
            stdout=json.dumps({'error': 'no issues found matching the provided IDs', 'schema_version': 1}))
        reply, _ = self.execute([], args=['pp-9'], show=gone)
        self.assertEqual(json.loads(reply['stdout'])['anchors'], [])
        # Any other failure still fails closed.
        broken = lambda command: types.SimpleNamespace(returncode=1, stdout='', stderr='database locked\n')
        with self.assertRaises(ValueError):
            self.execute([], args=['pp-9'], show=broken)

    def test_anchors_takes_at_most_the_cap_of_distinct_task_ids(self):
        for args in (['pp-%d' % index for index in range(rc.ANCHOR_READ_IDS_MAX + 1)],
                     ['pp-1', 'pp-1'], ['--all'], [''], ['pp 1'], 'pp-1', [1]):
            with self.subTest(args=args), self.assertRaisesRegex(ValueError, 'distinct task ids'):
                self.execute([], args=args)


try:
    from test_http_review_fixes import STUB, EndpointCase
except Exception:  # pragma: no cover - harness import problems surface in its own module
    EndpointCase = None


if EndpointCase is not None:
    class CountingEndpointBackend(http_service.EndpointBackend):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.calls = []

        def _endpoint(self, action, project, actor, args, *rest, **kwargs):
            self.calls.append((action, tuple(args) if action == 'anchors' else tuple(args[:1])))
            reply = super()._endpoint(action, project, actor, args, *rest, **kwargs)
            if action == 'bd' and args[:1] in (['list'], ['show']) and reply.get('returncode') == 0:
                # Real `bd list`/`bd show` print no comments (the stub's rows carry
                # them), so the anchors read is what must hide the anchors here.
                rows = json.loads(reply['stdout'])
                strip = lambda row: {k: v for k, v in row.items() if k != 'comments'}
                rows = [strip(row) for row in rows] if isinstance(rows, list) else strip(rows)
                reply = dict(reply, stdout=json.dumps(rows))
            return reply


@unittest.skipIf(EndpointCase is None, 'canonical stub harness unavailable')
class AnchorReadCostTests(EndpointCase if EndpointCase else unittest.TestCase):
    """kittrial-5bb.71: one anchors read per snapshot, however many labelled rows."""

    def make_backend(self):
        self.canonical_root = self.tmp / 'canonical'
        return CountingEndpointBackend(sys.executable, str(STUB), str(self.canonical_root),
                                       service=self.service)

    def seed(self, changes):
        path = self.canonical_root / 'canonical.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        for row in state['rows']:
            if row['id'] in changes:
                row.update(changes[row['id']])
        path.write_text(json.dumps(state), encoding='utf-8')

    def test_a_list_with_many_labelled_rows_costs_one_anchors_read(self):
        alex, project = self.setup_project()
        ids = []
        for index in range(14):
            created = self.create_task(alex, project, 'task %d' % index)
            self.assertEqual(201, created.status, created.data)
            ids.append(created.data['id'])
        kinds = [('reference', 'Kind: reference-entry-v1\n{}'),
                 ('proposal', 'Kind: requirement-proposal-v1\n{}'),
                 ('contribution-settings', 'Kind: contribution-settings-v1\n{}'),
                 ('capability', 'Kind: capability-entry-v1\n{}')]
        changes = {}
        for index, task in enumerate(ids[:8]):
            label, text = kinds[index % 4]
            changes[task] = {'labels': [label], 'status': 'closed',
                             'comments': [{'id': str(100 + index), 'text': text,
                                           'created_at': '2026-01-01T00:00:00Z'}]}
        for task in ids[8:12]:
            changes[task] = {'labels': [kinds[0][0] if task == ids[8] else 'proposal']}
        self.seed(changes)
        self.backend.calls = []
        listed = self.request('GET', '/v1/projects/%s/tasks?limit=100' % project, token=alex)
        self.assertEqual(200, listed.status, listed.data)
        self.assertEqual({item['id'] for item in listed.data['items']}, set(ids[8:]))
        actions = [call[0] for call in self.backend.calls]
        self.assertEqual(actions.count('anchors'), 1)
        # Few labelled rows: the one read names exactly them (bd show, no export).
        named = [call[1] for call in self.backend.calls if call[0] == 'anchors']
        self.assertEqual(sorted(named[0]), sorted(ids[:12]))
        self.assertNotIn(('bd', ('comments',)), self.backend.calls)
        self.assertEqual(sorted(actions), sorted(['bd', 'work', 'anchors']))
        # The single-task route reads one row's comments, once, and refuses the anchor.
        self.backend.calls = []
        refused = self.request('GET', '/v1/projects/%s/tasks/%s' % (project, ids[0]), token=alex)
        self.assertEqual(404, refused.status, refused.data)
        self.assertEqual(self.backend.calls.count(('bd', ('comments',))), 1)

    def test_more_labelled_rows_than_the_cap_cost_one_export_read(self):
        alex, project = self.setup_project()
        anchor = self.create_task(alex, project, 'anchor').data['id']
        path = self.canonical_root / 'canonical.json'
        state = json.loads(path.read_text(encoding='utf-8'))
        for row in state['rows']:
            if row['id'] == anchor:
                row.update(labels=['capability'], status='closed', comments=[
                    {'id': '900', 'text': 'Kind: capability-entry-v1\n{}', 'created_at': '2026-01-01T00:00:00Z'}])
        extra = ['kittrial-5bb.%d' % (9000 + index) for index in range(rc.ANCHOR_READ_IDS_MAX)]
        state['rows'].extend({'id': task, 'title': 'labelled', 'description': '', 'status': 'open',
                              'assignee': None, 'issue_type': 'task', 'comments': [],
                              'labels': ['proposal'], 'dependencies': [],
                              'created_at': '2026-01-01T00:00:00Z'} for task in extra)
        path.write_text(json.dumps(state), encoding='utf-8')
        self.backend.calls = []
        listed = self.request('GET', '/v1/projects/%s/tasks?limit=100' % project, token=alex)
        self.assertEqual(200, listed.status, listed.data)
        self.assertEqual(listed.data['total'], len(extra))
        self.assertNotIn(anchor, {item['id'] for item in listed.data['items']})
        self.assertEqual([call for call in self.backend.calls if call[0] == 'anchors'], [('anchors', ())])

    def test_a_list_without_labelled_rows_makes_no_anchors_read(self):
        alex, project = self.setup_project()
        for index in range(3):
            self.assertEqual(201, self.create_task(alex, project, 'plain %d' % index).status)
        self.backend.calls = []
        listed = self.request('GET', '/v1/projects/%s/tasks' % project, token=alex)
        self.assertEqual(200, listed.status, listed.data)
        self.assertEqual(sorted(call[0] for call in self.backend.calls), ['bd', 'work'])


class ScopeAndConfigTests(unittest.TestCase):
    def test_the_proposals_scope_is_recognised_and_grants_only_the_proposal_write(self):
        # Slice 0 recognised the scope with no capability; slice 1b (kittrial-5bb.70) maps
        # it to the proposal write and to nothing else. No credential ever approves.
        self.assertIn('proposals', http_authority.CREDENTIAL_SCOPES)
        self.assertEqual(http_authority.SCOPE_CAPABILITIES['proposals'],
                         frozenset({http_authority.CAP_PROPOSALS}))
        self.assertEqual(http_authority.CAP_PROPOSALS, 'proposals.write')
        self.assertNotIn(http_authority.CAP_PROPOSALS, http_authority.CREDENTIAL_FORBIDDEN_CAPABILITIES)
        self.assertIn(http_authority.CAP_APPROVE, http_authority.CREDENTIAL_FORBIDDEN_CAPABILITIES)
        self.assertNotIn(http_authority.CAP_PROPOSALS, http_authority.ROLE_CAPABILITIES['viewer'])
        for role in ('contributor', 'owner'):
            self.assertIn(http_authority.CAP_PROPOSALS, http_authority.ROLE_CAPABILITIES[role])

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

    def test_only_the_guard_and_the_landed_writers_know_the_record_prefixes(self):
        # The .41 slice 1 writer (kittrial-5bb.66) owns the reference family; the .60
        # slice 1a writer (kittrial-5bb.67) owns the capability entry, acceptance and
        # alias records; the slice 1b writer (kittrial-5bb.69) owns capability
        # verification; the .58 slice 1a writer (kittrial-5bb.68) owns proposals,
        # dispositions and contribution settings. recovery.py names the reference and
        # capability kinds as operator void targets (kittrial-5bb.74); it writes none of
        # those records, only a `record-void-v1` comment that names one.
        writers = {'recovery.py': ('Kind: reference-', 'Kind: reference-entry-v1', 'Kind: reference-acceptance-v1',
                                   'Kind: capability-', 'Kind: capability-entry-v1',
                                   'Kind: capability-acceptance-v1', 'Kind: capability-verification-v1',
                                   'Kind: capability-alias-v1'),
                   'reference_records.py': ('Kind: reference-', 'Kind: reference-entry-v1',
                                            'Kind: reference-acceptance-v1'),
                   'proposal_records.py': ('Kind: requirement-proposal-', 'Kind: proposal-disposition-',
                                           'Kind: contribution-settings-', 'Kind: requirement-proposal-v1',
                                           'Kind: proposal-disposition-v1', 'Kind: contribution-settings-v1'),
                   'capability_records.py': ('Kind: capability-', 'Kind: capability-entry-v1',
                                             'Kind: capability-acceptance-v1', 'Kind: capability-alias-v1'),
                   'capability_verification.py': ('Kind: capability-', 'Kind: capability-verification-v1')}
        offenders = []
        for path in sorted(KIT.glob('*.py')):
            text = path.read_text(encoding='utf-8')
            for marker in RECORD_MARKERS + tuple(prefix.strip() for prefix in PREFIX_WRITERS):
                if marker in text and path.name != 'reserved_comments.py' \
                        and marker not in writers.get(path.name, ()):
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
