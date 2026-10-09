"""Tracker JSON strings may contain Unicode line separators; only LF frames rows."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
import activity
import export_requirements
import native
import record_json
import reserved_comments

SEPARATORS = ('\u0085', '\u2028', '\u2029')
CONTROL = ('\r', '\n', '\v', '\f', '\x1c', '\x1d', '\x1e')


def row(text, ident='p-1'):
    return {'id': ident, 'title': 'before' + text + 'after',
            'description': 'brackets [ { " quote \\ ' + text,
            'comments': [{'id': 'c1', 'author': 'commenter', 'text': text}],
            'labels': ['label' + text], 'created_by': 'author', 'assignee': 'assignee'}


def wire(rows, ending='\n', final=True):
    return ending.join(json.dumps(r, ensure_ascii=False) for r in rows) + (ending if final else '')


class ExportFramingTests(unittest.TestCase):
    def test_valid_strings_survive_all_export_stream_shapes(self):
        for character in SEPARATORS + CONTROL:
            expected = [row(character), row('healthy', 'p-2')]
            for ending in ('\n', '\r\n'):
                for final in (False, True):
                    with self.subTest(character=repr(character), ending=repr(ending), final=final):
                        self.assertEqual(expected, record_json.loads_rows(wire(expected, ending, final)))

    def test_iterable_rows_remain_supported_and_empty_lines_do_not_add_rows(self):
        expected = [row('\u0085')]
        self.assertEqual(expected, record_json.loads_rows(['', json.dumps(expected[0], ensure_ascii=False), ' ']))
        self.assertEqual(expected, record_json.loads_rows('\n\r\n' + wire(expected) + '\n'))
        self.assertEqual([], record_json.loads_rows(''))

    def test_illegal_control_or_cut_row_is_marked_without_losing_healthy_neighbor(self):
        healthy = row('healthy', 'p-2')
        for bad in ('{"id":"p-1","title":"bad\rstring"}', '{"id":"p-1","title":"cut', 'not JSON'):
            with self.subTest(bad=repr(bad)):
                found = record_json.loads_rows(bad + '\n' + wire([healthy]))
                self.assertEqual(2, len(found))
                self.assertTrue(found[0]['malformed'])
                self.assertEqual(healthy, found[1])
                self.assertIsNone(found[0]['assignee'])
                self.assertEqual([], found[0]['labels'])

    def test_nested_row_limit_and_record_limit_are_unchanged(self):
        self.assertEqual((64, 750), (record_json.NESTING_MAX, record_json.ROW_NESTING_MAX))
        text = '{"id":"p-1","title":"a\u0085b","nested":' + '[' * 65 + '0' + ']' * 65 + '}'
        self.assertFalse(record_json.loads_rows(text)[0].get('malformed'))
        with self.assertRaises(record_json.NestingError):
            record_json.loads(text)
        too_deep = '{"id":"p-1","nested":' + '[' * 751 + '0' + ']' * 751 + '}'
        self.assertTrue(record_json.loads_rows(too_deep)[0]['malformed'])

    def test_native_two_row_stdout_keeps_every_row_and_no_unicode_noise(self):
        for character in SEPARATORS + CONTROL:
            expected = [row(character), row('healthy', 'p-2')]
            with self.subTest(character=repr(character)):
                data, noise = native.json_stdout(wire(expected))
                self.assertEqual(expected, record_json.loads_rows(data))
                self.assertEqual([], noise)

    def test_native_warning_is_one_physical_line(self):
        for character in SEPARATORS:
            with self.subTest(character=repr(character)):
                warning = 'notice before' + character + 'after'
                data, noise = native.json_stdout(warning + '\n' + wire([row('healthy')]))
                self.assertEqual([row('healthy')], record_json.loads_rows(data))
                self.assertEqual([warning], noise)

    def test_offline_activity_and_requirements_read_the_same_complete_rows(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'issues.jsonl'
            for character in SEPARATORS + CONTROL:
                expected = [row(character), row('healthy', 'p-2')]
                with self.subTest(character=repr(character)):
                    source.write_text(wire(expected), encoding='utf-8')
                    self.assertEqual(expected, activity.load_export(source))
                    self.assertEqual(expected, export_requirements.read_export(source))

    def test_offline_readers_still_refuse_cut_or_illegal_json(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'issues.jsonl'
            for bad in ('{"id":"p-1","title":"bad\rstring"}', '{"id":"p-1",', 'not JSON'):
                source.write_text(wire([row('healthy')]) + bad, encoding='utf-8')
                for read in (activity.load_export, export_requirements.read_export):
                    with self.subTest(bad=repr(bad), reader=read.__module__), self.assertRaises(ValueError):
                        read(source)

    def test_dependency_jsonl_is_not_split_inside_a_string(self):
        for character in SEPARATORS:
            with self.subTest(character=repr(character)):
                body = wire([{'issue_id': 'p-1', 'depends_on_id': 'p-2', 'note': character}])
                self.assertEqual(['p-1', 'p-2'], reserved_comments._edge_ids(body))

    def test_non_lf_row_separators_are_not_tracker_jsonl(self):
        for delimiter in ('\u0085', '\u2028', '\f'):
            with self.subTest(delimiter=repr(delimiter)):
                text = delimiter.join(json.dumps(r) for r in [row('one'), row('two', 'p-2')])
                self.assertTrue(any(r.get('malformed') for r in record_json.loads_rows(text)))

    def test_native_refusal_keeps_unicode_in_its_one_error_line(self):
        import bd_refusals
        message = 'cannot find before\u0085after'
        self.assertEqual(message, bd_refusals.said(1, '', 'notice\nError: '+message+'\n'))

    def test_capability_lifecycle_export_keeps_the_whole_unicode_row(self):
        import capability_verification as verification
        expected = dict(row('\u0085'), issue_type='event')
        def run(args):
            self.assertEqual(args, ['export', '--all'])
            return wire([expected])
        with patch.object(verification, 'integrated_commits', return_value={'a'*40}) as consume:
            reader = verification.Integrated(run)
            reader._export()
            self.assertEqual(reader.everything, {'a'*40})
            self.assertEqual(consume.call_args.args[0], [expected])


@unittest.skipIf(os.name == 'nt', 'endpoint uses the native POSIX lock; Linux route tests cover it')
class ReaderRouteTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.project = self.root / 'projects' / 'pp'
        (self.project / '.beads').mkdir(parents=True)
        (self.project / '.beads' / 'metadata.json').write_text(json.dumps({
            'dolt_server_host': '127.0.0.1', 'dolt_server_port': 12345,
            'dolt_server_user': 'root', 'dolt_database': 'pp'}), encoding='utf-8')

    def test_tracker_names_survive_unicode_and_truly_unreadable_rows_still_refuse(self):
        import actor_names
        import admin
        import endpoint
        slot = {'id': 'pp-merge-slot', 'labels': ['gt:slot']}
        for character in SEPARATORS:
            good = wire([slot, row(character), row('healthy', 'p-2')])
            with self.subTest(character=repr(character)), patch.object(admin, 'run_bd', return_value=good):
                self.assertEqual({'author', 'assignee', 'commenter'}, set(endpoint.tracker_actors(self.root, self.project)))
            for bad in ('{"id":"broken",', 'not JSON', '{"id":"p-3","title":"bad\rstring"}'):
                with self.subTest(character=repr(character), bad=repr(bad)), patch.object(admin, 'run_bd', return_value=good + bad):
                    with self.assertRaises(actor_names.TrackerUnreadable):
                        endpoint.tracker_actors(self.root, self.project)

    def test_tracker_whole_read_faults_and_fake_slot_stay_refused(self):
        import actor_names
        import admin
        import endpoint
        for text in ('', wire([row('\u0085')])):
            with self.subTest(text=repr(text)), patch.object(admin, 'run_bd', return_value=text):
                with self.assertRaises(actor_names.TrackerMergeSlotMissing):
                    endpoint.tracker_actors(self.root, self.project)
        for text in ('{"id":"p-merge-slot", junk}\n',):
            with self.subTest(text=repr(text)), patch.object(admin, 'run_bd', return_value=text):
                with self.assertRaises(actor_names.TrackerUnreadable):
                    endpoint.tracker_actors(self.root, self.project)
        with patch.object(admin, 'run_bd', side_effect=subprocess.CalledProcessError(1, ['bd'])):
            with self.assertRaises(actor_names.TrackerUnreadable):
                endpoint.tracker_actors(self.root, self.project)

    def test_credential_listing_reads_unicode_collision_and_reports_cut_rows_as_unknown(self):
        import admin
        import sessions
        state = self.root / 'synthetic-service.json'
        state.write_text(json.dumps({'users': {}, 'credentials': {'credential-one': {
            'user_id': 'usr_a', 'project_id': 'pp', 'actor': 'commenter', 'revoked': False}}}), encoding='utf-8')
        healthy = wire([row('\u0085')])
        with patch.object(sessions, 'registered_actors', return_value=[]):
            with patch.object(admin, 'run_bd', return_value=healthy):
                item = admin.credential_actors(self.root, state)['credentials'][0]
            self.assertIs(item['tracker_rows'], True, item)
            self.assertEqual(item['collides'], "a name this project's tracker already holds", item)
            self.assertIs(item['refused_when_it_writes'], True, item)
            with patch.object(admin, 'run_bd', return_value=healthy+'{"id":"cut",'):
                item = admin.credential_actors(self.root, state)['credentials'][0]
            self.assertIsNone(item['tracker_rows'], item)
            self.assertEqual(item['unreadable_rows'], ['cut'], item)

    def test_adoption_reader_keeps_names_from_unicode_rows(self):
        import admin
        import sessions
        for character in SEPARATORS:
            with self.subTest(character=repr(character)), patch.object(admin, 'run_bd', return_value=wire([row(character)])), \
                    patch.object(sessions, 'read_registry', return_value={'records': {}}), patch.object(sessions, 'owner_map', return_value={}):
                self.assertEqual({'author', 'assignee', 'commenter'}, admin.project_actor_names(Path('/unused'), 'p', Path('/unused/p')))

    def test_keyed_catalog_export_above_show_threshold_keeps_all_rows(self):
        import keyed_entries
        expected = [dict(row('\u0085', 'p-%d' % i), labels=['ref']) for i in range(keyed_entries.CATALOG_SHOW_MAX + 1)]
        calls = []
        def run(args):
            calls.append(args)
            return wire(expected) if args[0] == 'export' else json.dumps(expected, ensure_ascii=False)
        found = keyed_entries.read_labelled(run, 'ref')
        self.assertEqual(expected, found)
        self.assertEqual(['list', 'export'], [a[0] for a in calls])

    def test_capability_catalog_keeps_unicode_anchor_without_membership_fallback(self):
        import capability_records
        import keyed_entries
        expected = [dict(row('\u2028', 'p-%d' % i), labels=[capability_records.TYPE_LABEL]) for i in range(keyed_entries.CATALOG_SHOW_MAX + 1)]
        calls = []
        def run(args):
            calls.append(args)
            return wire(expected) if args[0] == 'export' else json.dumps(expected, ensure_ascii=False)
        found, events = capability_records.read_catalog(run)
        self.assertEqual(expected, found)
        self.assertEqual([], events)
        self.assertEqual(['list', 'export'], [a[0] for a in calls])

    def test_http_canonical_jsonl_fallback_keeps_last_complete_unicode_document(self):
        import http_service
        for character in SEPARATORS:
            expected = row(character)
            with self.subTest(character=repr(character)):
                self.assertEqual(expected, http_service._canonical_payload('notice\n' + wire([expected])))


if __name__ == '__main__':
    unittest.main()
