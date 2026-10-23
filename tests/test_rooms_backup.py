"""The separate rooms backup file: complete, verified, preferred/fallback and restored (kittrial-5bb.385)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import admin
import rooms


class RoomsBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'runtime'
        self.root.mkdir()
        (self.root / 'backups').mkdir()
        self.project = self.root / 'projects' / 'alpha'
        self.project.mkdir(parents=True)
        self.actor = {'id': 'usr_0123456789abcdef', 'name': 'Alice', 'role': 'coordinator'}
        self.room = rooms.create(self.project, 'Planning', self.actor, rooms.LIMIT_DEFAULTS)
        rooms.post(self.project, self.room['id'], 'carried', 'op-1', self.actor, rooms.LIMIT_DEFAULTS)

    def write_record(self, name='alpha', path=None):
        record = rooms.backup_record(self.project)
        (path or (self.root / 'backups' / (name + '.rooms.json'))).write_text(
            json.dumps(record, sort_keys=True), encoding='utf-8')
        return record

    def test_outcome_prefers_canonical_and_falls_back_to_last_complete(self):
        canonical = self.root / 'backups' / 'alpha.rooms.json'
        self.write_record(path=canonical)
        outcome, path, data, problem = admin.rooms_backup_outcome(self.root, 'alpha')
        self.assertEqual(('answer', canonical), (outcome, path))
        self.assertIsNone(problem)
        canonical.unlink()
        last = admin.last_complete_rooms_path(self.root, 'alpha')
        self.write_record(path=last)
        outcome, path, data, problem = admin.rooms_backup_outcome(self.root, 'alpha')
        self.assertEqual(('answer', last), (outcome, path))
        self.assertIsNone(problem)
        last.unlink()
        self.assertEqual(('absent', None, None, None), admin.rooms_backup_outcome(self.root, 'alpha'))

    def test_damaged_rooms_file_refuses_and_restore_can_be_skipped(self):
        path = self.root / 'backups' / 'alpha.rooms.json'
        record = self.write_record(path=path)
        broken = json.loads(json.dumps(record))
        first = next(name for name in broken['files'] if name != 'index.json')
        broken['files'][first]['base64'] = 'AAAA'
        path.write_text(json.dumps(broken), encoding='utf-8')
        outcome, _, _, problem = admin.rooms_backup_outcome(self.root, 'alpha')
        self.assertEqual('refuse', outcome)
        self.assertIn(first, problem)
        destination = self.root / 'projects' / 'copy'
        destination.mkdir()
        with self.assertRaises(ValueError):
            admin.restore_rooms(self.root, 'alpha', 'copy')
        line = admin.restore_rooms(self.root, 'alpha', 'copy', without_rooms=True)
        self.assertIn('NOT restored (--without-rooms)', line)
        self.assertFalse((destination / rooms.FOLDER).exists())

    def test_restore_writes_verified_files_and_refuses_a_live_folder(self):
        record = self.write_record()
        destination = self.root / 'projects' / 'copy'
        destination.mkdir()
        line = admin.restore_rooms(self.root, 'alpha', 'copy')
        self.assertIn('Restored', line)
        read = rooms.read_messages(destination, self.room['id'])
        self.assertEqual('carried', read['messages'][0]['text'])
        # A second restore into the now-populated folder is refused, never overwritten.
        with self.assertRaises(ValueError):
            admin.restore_rooms(self.root, 'alpha', 'copy')

    def test_a_rooms_file_is_not_a_coordination_path(self):
        # The old kit's fixed restore list has not changed: rooms ride outside the sidecar, so
        # a rollback ignores the file and its restore still works (the rollback boundary).
        admin.validate_coordination_files({})
        record = self.write_record()
        self.assertNotIn('.rooms.json', record['files'])
        self.assertTrue(all(name == 'index.json' or name.split('/')[0].startswith('r-')
                            for name in record['files']))
        # And the separate file name is the one design section 2 names.
        self.assertTrue((self.root / 'backups' / 'alpha.rooms.json').is_file())
        self.assertTrue(admin.last_complete_rooms_path(self.root, 'alpha').name.endswith('.rooms.last-complete.json'))


if __name__ == '__main__':
    unittest.main()
