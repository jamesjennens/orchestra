"""Exact native bindings and the optional host family, before route wiring."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import web_lifecycle_marks as marks
from test_lifecycle_voids import rows


class MarkFamilyTests(unittest.TestCase):
    def setUp(self):
        self.scratch = tempfile.TemporaryDirectory()
        self.addCleanup(self.scratch.cleanup)
        self.root = Path(self.scratch.name)
        self.rows = rows()
        self.event = self.rows[-1]
        self.authority = dict(route=marks.route_for('p', 'p-task'),
                              capability='coordinate', project='p')

    def issue(self):
        return marks.issue(self.root, 'p', self.event, self.authority, enabled=True)

    def test_writer_defaults_off_and_rejects_truthy_non_boolean_switch(self):
        for value in (False, None, 1, 'on'):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'writer is OFF'):
                marks.issue(self.root, 'p', self.event, self.authority, enabled=value)
            self.assertEqual(list(self.root.iterdir()), [])
        with self.assertRaisesRegex(ValueError, 'writer is OFF'):
            marks.issue(self.root, 'p', self.event, self.authority)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_reads_and_legacy_backup_create_no_family(self):
        self.assertFalse(marks.read_journal(self.root)['present'])
        self.assertEqual(marks.backup_files(self.root, self.rows), {})
        self.assertIsNone(marks.validate_backup({}, self.rows))
        self.assertTrue(marks.recorder_eligible(self.event, 'p', ['op']))
        self.assertFalse(marks.recorder_eligible(self.event, 'p', []))
        self.assertEqual(list(self.root.iterdir()), [])

    @unittest.skipIf(os.name == 'nt', 'POSIX modes are not Windows ACLs')
    def test_private_creation_and_writable_family_refusal(self):
        before_umask=os.umask(0o002)
        try:
            self.issue()
        finally:
            os.umask(before_umask)
        directory=self.root / marks.DIRECTORY
        member=next(directory.iterdir())
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(member.stat().st_mode & 0o777, 0o600)
        member.chmod(0o620)
        report=marks.read_journal(self.root)
        self.assertTrue(any('group- or world-writable' in e for e in report['invalid']))
        self.assertFalse(marks.recorder_eligible(self.event,'p',[],{},report))
        with self.assertRaises(ValueError): marks.backup_files(self.root,self.rows)
        member.chmod(0o600)
        directory.chmod(0o702)
        report=marks.read_journal(self.root)
        self.assertTrue(any('group- or world-writable' in e for e in report['invalid']))
        self.assertFalse(marks.recorder_eligible(self.event,'p',[],{},report))

    def test_issued_mark_survives_operator_replacement_and_exact_retry(self):
        entry = self.issue()
        before = {f.name: f.read_bytes() for f in (self.root / marks.DIRECTORY).iterdir()}
        self.assertEqual(self.issue(), entry)
        self.assertEqual(before, {f.name: f.read_bytes() for f in (self.root / marks.DIRECTORY).iterdir()})
        journal = marks.read_journal(self.root)
        self.assertFalse(journal['invalid'])
        self.assertTrue(marks.recorder_eligible(self.event, 'p', ['replacement'], {}, journal))
        self.assertFalse(marks.recorder_eligible(self.event, 'another-project', [], {}, journal))
        self.assertFalse(marks.recorder_eligible(None, 'p', [], {}, journal))

    def test_every_native_binding_change_loses_marked_eligibility(self):
        entry = self.issue()
        changes = {'id': 'p-task.3', 'created_by': 'replacement',
                   'created_at': '2026-10-02T00:00:02Z', 'title': 'renamed event',
                   'description': self.event['description'] + ' altered',
                   'dependencies': [dict(type='parent-child', depends_on_id='another-task')]}
        for key, value in changes.items():
            with self.subTest(field=key):
                event = copy.deepcopy(self.event)
                event[key] = value
                self.assertFalse(marks.matches(entry, 'p', event))
        self.assertFalse(marks.matches(entry, 'p', None))

    def test_descriptor_is_exact_and_not_written_into_native_payload(self):
        native = copy.deepcopy(self.event)
        for key, value in [('route', '/other'), ('capability', 'tasks.write'), ('project', 'other')]:
            with self.subTest(field=key), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                authority = dict(self.authority)
                authority[key] = value
                with self.assertRaises(ValueError):
                    marks.issue(root, 'p', self.event, authority, enabled=True)
                self.assertEqual(list(root.iterdir()), [])
        self.issue()
        self.assertEqual(self.event, native)

    def test_record_hash_path_and_native_restore_context_are_all_checked(self):
        entry = self.issue()
        files = marks.backup_files(self.root, self.rows)
        self.assertEqual(marks.validate_backup(files, self.rows)['count'], 1)
        altered = copy.deepcopy(files)
        name = next(n for n in altered if n.startswith(marks.DIRECTORY + '/'))
        altered[name]['record']['created_at'] = '2026-10-02T00:00:02Z'
        with self.assertRaises(ValueError):
            marks.validate_backup(altered, self.rows)
        changed_rows = copy.deepcopy(self.rows)
        changed_rows[-1]['created_at'] = '2026-10-02T00:00:02Z'
        with self.assertRaises(ValueError):
            marks.validate_backup(files, changed_rows)
        absent = dict(files)
        absent.pop(name)
        with self.assertRaises(ValueError):
            marks.validate_backup(absent, self.rows)
        with self.assertRaises(ValueError):
            marks.validate_entry(entry, '0' * 64 + '.json')

    def test_damage_never_confers_mark_authority_or_revokes_legacy_operator(self):
        self.issue()
        (self.root / marks.DIRECTORY / 'bad.json').write_text('{}')
        journal = marks.read_journal(self.root)
        self.assertTrue(journal['invalid'])
        self.assertFalse(marks.recorder_eligible(self.event, 'p', [], {}, journal))
        self.assertTrue(marks.recorder_eligible(self.event, 'p', ['op'], {}, journal))
        with self.assertRaises(ValueError):
            marks.backup_files(self.root, self.rows)
        with self.assertRaises(ValueError):
            self.issue()

    def test_restore_marker_reports_uncertainty_before_writing(self):
        (self.root / marks.RESTORE_MARKER).write_text('{}')
        self.assertTrue(marks.read_journal(self.root)['invalid'])
        with self.assertRaises(ValueError):
            self.issue()
        self.assertFalse((self.root / marks.DIRECTORY).exists())

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'POSIX nonblocking FIFO family read')
    def test_fifo_member_is_refused_without_opening_for_a_blocking_read(self):
        directory = self.root / marks.DIRECTORY
        directory.mkdir()
        os.mkfifo(directory / ('0' * 64 + '.json'))
        self.assertTrue(marks.read_journal(self.root)['invalid'])


if __name__ == '__main__':
    unittest.main()
