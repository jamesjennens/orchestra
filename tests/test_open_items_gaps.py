"""Open items slice 0 gaps (kittrial-5bb.171): the metadata mode guard on a real tracker, the
record-family lookalikes on the raw comment route, and a deeply nested sidecar."""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import admin
import record_json
import reserved_comments as rc
import test_bd_label_aliases as rb
from test_merge_slot_real_bd import without_inherited_tests
from test_task_files_ops_real_bd import TaskFilesOpsRealBdTests as Fixture

SERVER = {'dolt_mode': 'server', 'dolt_server_host': '127.0.0.1', 'dolt_server_port': 3307,
          'dolt_server_user': 'root', 'dolt_database': 'alpha'}


class MetadataModeTests(unittest.TestCase):
    """project_metadata_state is `server` only for dolt_mode server with the coordinates."""

    def state(self, data):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'projects' / 'alpha' / '.beads').mkdir(parents=True)
            (root / 'projects' / 'alpha' / '.beads' / 'metadata.json').write_text(json.dumps(data), encoding='utf-8')
            return admin.project_metadata_state(root, 'alpha')

    def test_only_server_mode_is_a_server_project(self):
        self.assertEqual(self.state(SERVER), 'server')
        for mode in ('embedded', 'Server', None, '', 1):
            with self.subTest(mode=mode):
                self.assertEqual(self.state(dict(SERVER, dolt_mode=mode)), 'unreadable')
        self.assertEqual(self.state({key: value for key, value in SERVER.items() if key != 'dolt_mode'}), 'unreadable')

    def test_the_sentence_names_the_fault(self):
        self.assertIn('does not record the Dolt server coordinates', admin.server_metadata_sentence({}))
        noted = admin.server_metadata_sentence({key: SERVER[key] for key in admin.SERVER_COORDINATES})
        self.assertIn('has no dolt_mode', noted)
        self.assertIn('has dolt_mode "Server"', admin.server_metadata_sentence(dict(SERVER, dolt_mode='Server')))
        self.assertIsNone(admin.server_metadata_fault(SERVER))

    def test_server_coordinates_are_still_read_without_the_mode(self):
        # The SQL-coordinate callers do not need dolt_mode: project_server_metadata is unchanged for them.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'projects' / 'alpha' / '.beads').mkdir(parents=True)
            data = {key: value for key, value in SERVER.items() if key != 'dolt_mode'}
            (root / 'projects' / 'alpha' / '.beads' / 'metadata.json').write_text(json.dumps(data), encoding='utf-8')
            self.assertEqual(admin.project_server_metadata(root, 'alpha'), ('127.0.0.1', 3307, 'root', 'alpha'))


@without_inherited_tests
class ModeGuardRealBdTests(Fixture):
    """On a real bd, a project whose metadata says dolt_mode embedded is never read by bd."""

    def embed(self, name):
        path = self.new_project(name) / '.beads' / 'metadata.json'
        data = json.loads(path.read_text(encoding='utf-8'))
        self.assertEqual(data['dolt_mode'], 'server')
        data['dolt_mode'] = 'embedded'
        path.write_text(json.dumps(data), encoding='utf-8')
        return path.parent

    def test_each_caller_refuses_an_embedded_mode_project_and_creates_nothing(self):
        beads = self.embed('gapone')
        self.assertEqual(admin.project_metadata_state(self.root, 'gapone'), 'unreadable')
        with mock.patch.object(admin, 'run_bd', side_effect=AssertionError('bd must not run')) as ran:
            self.assertEqual(admin.restore_destination_state(self.root, 'gapone'), 'partial')
            findings = admin.retire_findings(self.root, 'gapone')
            self.assertEqual((findings['metadata'], findings['bd']), ('unreadable', 'unreachable'))
            code, out, err = self.main('open-item-label-check', 'gapone')
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out)['unreadable'], ['gapone'])
            ran.assert_not_called()
        self.assertFalse((beads / 'embeddeddolt').exists())

    def test_the_endpoint_and_merge_slot_guards_never_run_bd_on_an_embedded_mode_project(self):
        import actor_names
        import endpoint
        beads = self.embed('gapthree')
        project = self.root / 'projects' / 'gapthree'
        self.assertEqual(admin.project_merge_slot_state(self.root, 'gapthree'), {'state': 'unknown', 'detail': None})
        with self.assertRaises(actor_names.TrackerUnreadable):
            endpoint.tracker_actors(self.root, project)
        self.assertFalse((beads / 'embeddeddolt').exists())
        # The operator's sentence says what is wrong: the mode, not the coordinates.
        said = admin.server_metadata_sentence(json.loads((beads / 'metadata.json').read_text(encoding='utf-8')))
        self.assertIn('records the Dolt server coordinates but has dolt_mode "embedded"', said)
        code, out, err = self.main('open-item-label-check', 'gapthree')
        self.assertIn('has dolt_mode "embedded"', json.loads(out)['projects']['gapthree']['error'])

    def test_a_server_mode_project_is_still_read(self):
        self.new_project('gaptwo')
        self.assertEqual(admin.project_metadata_state(self.root, 'gaptwo'), 'server')
        self.assertEqual(admin.restore_destination_state(self.root, 'gaptwo'), 'empty')
        code, out, err = self.main('open-item-label-check', 'gaptwo')
        self.assertEqual((code, json.loads(out)['using']), (0, []))


# Each family of record comments (reserved_comments.RECORD_COMMENT_FAMILIES) with a body that
# names it but is not a versioned prefix the kit writes.
LOOKALIKE_TAILS = ('v1', 'vx\n{}', 'v1x\n{}', 'v1 {}')


@unittest.skipUnless(hasattr(__import__('os'), 'mkfifo'), 'needs FIFOs')
class FifoMetadataTests(unittest.TestCase):
    def test_a_fifo_as_metadata_is_unreadable_and_does_not_block(self):
        import os
        import threading
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            beads = root / 'projects' / 'alpha' / '.beads'
            beads.mkdir(parents=True)
            os.mkfifo(str(beads / 'metadata.json'))
            answer = []
            worker = threading.Thread(
                target=lambda: answer.append((admin.project_metadata_state(root, 'alpha'),
                                              admin.project_server_metadata(root, 'alpha'))), daemon=True)
            worker.start()
            worker.join(20)
            self.assertFalse(worker.is_alive(), 'reading a FIFO as metadata blocked')
            self.assertEqual(answer, [('unreadable', None)])


class FamilyLookalikeTests(unittest.TestCase):
    def test_every_family_lookalike_is_hidden_and_refused_raw(self):
        self.assertTrue(rc.RECORD_COMMENT_FAMILIES)
        for family in rc.RECORD_COMMENT_FAMILIES:
            for tail in LOOKALIKE_TAILS:
                body = family + tail
                with self.subTest(body=body):
                    self.assertTrue(rc.is_record_comment(body))
                    self.assertIsNotNone(rc.reserved_match(body))
                    for source in ('positional', '--file'):
                        with self.assertRaisesRegex(ValueError, 'Refusing raw'):
                            rc.check_comment_body(body, source, actor='alice', task='kit-1')
                    with self.assertRaisesRegex(ValueError, 'Refusing raw'):
                        rc.check_raw_request(['comments', 'add', 'kit-1', body], {}, actor='alice', task='kit-1')

    def test_a_canonical_prefix_still_matches_its_own_entry_not_the_family_fallback(self):
        for kind in rc._RECORD_KIND_RESERVATIONS:
            if not any(('Kind: %s-' % kind).startswith(family) for family in rc.RECORD_COMMENT_FAMILIES):
                continue
            body = 'Kind: %s-v1\n{}' % kind
            with self.subTest(kind=kind):
                self.assertEqual(rc.reserved_match(body)[1:], rc._RECORD_KIND_RESERVATIONS[kind])

    def test_a_lookalike_has_no_legitimate_writer(self):
        for family in rc.RECORD_COMMENT_FAMILIES:
            self.assertFalse(rc.is_legitimate_writer(family + 'vx\n{}', actor='alice', task='kit-1'))

    def test_prose_that_names_no_family_is_unaffected(self):
        for body in ('Kind: note', 'Kind: bug', 'Kind: proposal-v1', 'Kind: reference', 'Kind: referenced-x',
                     'Kind: open-items-v1', 'Kind: owner-answers', 'Kind: capabilities', 'Kind:',
                     'Notes. Kind: open-item-vx', 'kind: open-item-vx'):
            with self.subTest(body=body):
                self.assertIsNone(rc.reserved_match(body))
                rc.check_comment_body(body, 'positional', actor='alice', task='kit-1')


class NestedSidecarTests(unittest.TestCase):
    """read_sidecar_json parses with record_json, whose bound (record_json.NESTING_MAX, record_json.py:25)
    holds on every Python: the outcome never depends on the interpreter's own recursion limit."""

    def read(self, depth):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'sidecar.json'
            path.write_text('[' * depth + ']' * depth, encoding='utf-8')
            return admin.read_sidecar_json(path)

    def test_a_sidecar_just_over_the_bound_is_a_problem_and_just_under_reads(self):
        limit = record_json.NESTING_MAX
        self.assertEqual(self.read(limit), (self.read(limit)[0], None))
        self.assertIsNotNone(self.read(limit)[0])
        data, problem = self.read(limit + 1)
        self.assertEqual((data, problem), (None, 'it is not valid JSON'))

    def test_a_very_deep_sidecar_is_still_a_problem(self):
        self.assertEqual(self.read(5000), (None, 'it is not valid JSON'))

    def test_the_recursion_error_backstop_still_answers(self):
        with mock.patch.object(admin.record_json, 'loads', side_effect=RecursionError):
            self.assertEqual(self.read(2), (None, 'it is not valid JSON'))


if __name__ == '__main__':
    unittest.main()
