"""Read-only capability propose validation and its client/endpoint split."""
import copy
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import capability_records as cr
import client
import requirement_records
from export_requirements import revision_comment
from test_capability_records import CapabilityNative, entry
from test_capability_wiring import ClientCase
from test_reference_records import OPERATOR


def without_operation(payload):
    return {name: value for name, value in payload.items() if name != 'operation'}


class ValidationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.project = Path(temp.name)
        self.native = CapabilityNative()

    def validate(self, document):
        return cr.validate(['validate', '@attachment:0'], {
            '0': {'flag': '--file', 'text': json.dumps(document)}}, self.native, [OPERATOR])

    def test_valid_payload_and_list_have_per_item_results_and_no_writes(self):
        first = without_operation(entry())
        second = without_operation(entry(operation_id='cap-2', key='review.second', name='Second'))
        result = self.validate([first, second])
        self.assertTrue(result['valid'])
        self.assertEqual(result['summary'], {'items': 2, 'valid': 2, 'invalid': 0, 'errors': 0})
        self.assertEqual([(item['index'], item['key'], item['state']) for item in result['items']],
                         [(0, 'review.structured-contribution', 'draft'), (1, 'review.second', 'draft')])
        self.assertEqual(self.native.writes(), [])
        self.assertFalse((self.project / '.capability-requests').exists())
        self.assertTrue(all(call[0] in ('list', 'show', 'export') for call in self.native.calls))

    def test_a_non_object_list_member_does_not_stop_later_items(self):
        valid = without_operation(entry())
        result = self.validate([valid, 'not an object', dict(valid, operation_id='cap-2', key='review.second')])
        self.assertEqual(result['summary'], {'items': 3, 'valid': 2, 'invalid': 1, 'errors': 1})
        self.assertEqual([item['index'] for item in result['items']], [0, 1, 2])
        self.assertIsNone(result['items'][1]['key'])
        self.assertEqual(result['items'][1]['errors'][0]['field'], 'payload')
        self.assertTrue(result['items'][2]['valid'])

    def test_all_independent_errors_have_fields_and_fix_hints(self):
        payload = without_operation(entry(schema_version=2, revision=True, expected_sha256='x', name='', summary='',
                                          owner='jjbp-j02', tags=['BAD'], aliases=['a\tb']))
        item = self.validate(payload)['items'][0]
        self.assertFalse(item['valid'])
        fields = {error['field'] for error in item['errors']}
        self.assertTrue({'schema_version', 'revision', 'expected_sha256', 'name', 'summary', 'owner', 'tags',
                         'aliases'} <= fields)
        self.assertEqual(item['errors'][0]['message'], 'schema_version must be the integer 1')
        for error in item['errors']:
            self.assertTrue(error['field'])
            self.assertTrue(error['message'])
            self.assertTrue(error['hint'])
        self.assertIn('account:<uid> or person:<name>', next(error['hint'] for error in item['errors']
                                                              if error['field'] == 'owner'))

    def test_validate_and_propose_agree_on_verdict_and_first_error(self):
        cases = [
            {},
            {'schema_version': 2},
            {'operation_id': 'bad id'},
            {'key': 'NotAKey'},
            {'unknown': True},
            {'name': ''},
            {'summary': 'x' * 1201},
            {'aliases': ['Same', 'same']},
            {'requirements': [{'key': 'R01'}]},
            {'anchors': ['docs/REVIEWS.md']},
            {'code': ['review_workflow.py']},
            {'tests': ['tests/x.py#heading']},
            {'owner': 'person:session-4e40fde3'},
            {'tags': ['x'] * 2},
            {'revision': 2},
            {'revision': True},
            {'revision': 1.0},
            {'revision': True, 'name': ''},
            {'expected_sha256': 'f' * 64},
            {'acceptance_state': 'accepted'},
            {'labels': ['capability:accepted']},
            {'name': '', 'owner': 'jjbp-j02', 'tags': ['BAD']},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                payload = entry(**changes)
                propose_native = CapabilityNative()
                propose_error = None
                try:
                    cr.apply_native(payload, 'alice', propose_native, self.project)
                except ValueError as error:
                    propose_error = str(error)
                item = self.validate(without_operation(payload))['items'][0]
                self.assertEqual(item['valid'], propose_error is None)
                if propose_error is not None:
                    self.assertEqual(item['errors'][0]['message'], propose_error)

    def test_current_key_and_requirement_preflight_match_propose(self):
        cr.apply_native(entry(), 'alice', self.native, self.project)
        payload = without_operation(entry(operation_id='cap-2'))
        item = self.validate(payload)['items'][0]
        with self.assertRaises(ValueError) as caught:
            cr.apply_native(entry(operation_id='cap-3'), 'alice', self.native, self.project)
        self.assertEqual(item['errors'][0]['message'], str(caught.exception))

        record = requirement_records.requirement_record({
            'kind': 'requirement', 'title': 'R01: Intent', 'key': 'R01', 'description': 'Statement.',
            'acceptance_state': 'draft'}, 'req-9', 1)
        self.native.seed('req-9', labels=['requirement', 'requirement:draft'],
                         comments=[{'id': 'r1', 'text': revision_comment(record), 'author': 'alice'}])
        good = without_operation(entry(operation_id='cap-4', key='review.requirement',
                                      requirements=[{'key': 'R01', 'revision': 1}]))
        self.assertTrue(self.validate(good)['items'][0]['valid'])
        bad = copy.deepcopy(good)
        bad['requirements'] = [{'key': 'R01', 'revision': 2}]
        item = self.validate(bad)['items'][0]
        with self.assertRaises(ValueError) as caught:
            cr.apply_native(entry(operation_id='cap-5', key='review.requirement',
                                  requirements=[{'key': 'R01', 'revision': 2}]), 'alice', self.native, self.project)
        self.assertEqual(item['errors'][0]['message'], str(caught.exception))

    def test_file_shape_and_operation_rules_are_shared(self):
        for document in ('text', 1, None):
            with self.subTest(document=document), self.assertRaises(ValueError):
                self.validate(document)
        wrapped = self.validate({'schema_version': 1, 'items': []})
        self.assertFalse(wrapped['items'][0]['valid'])
        payload = without_operation(entry())
        payload['operation'] = 'propose'
        with self.assertRaisesRegex(ValueError, 'must not set operation'):
            self.validate(payload)
        self.assertEqual(self.validate([])['summary'], {'items': 0, 'valid': 0, 'invalid': 0, 'errors': 0})
        with self.assertRaisesRegex(ValueError, 'at most 100'):
            self.validate([without_operation(entry())] * 101)

    def test_json_depth_and_duplicate_names_use_the_shared_reader(self):
        deep = json.dumps(without_operation(entry()))
        nested = '[' * 65 + '0' + ']' * 65
        duplicate = deep[:-1] + ',"name":"duplicate"}'
        for text in (nested, duplicate, '{not json'):
            with self.subTest(text=text[:30]), self.assertRaises(ValueError):
                cr.validate(['validate', '@attachment:0'], {'0': {'flag': '--file', 'text': text}},
                            self.native, [OPERATOR])


class ClientValidateTests(ClientCase):
    def setUp(self):
        super().setUp()
        self.native = CapabilityNative()
        self.project = self.root / 'project'
        self.project.mkdir()
        (self.repo / 'docs').mkdir()
        (self.repo / 'docs' / 'REVIEWS.md').write_text('# Structured reviews\n', encoding='utf-8')
        (self.repo / 'tests').mkdir()
        (self.repo / 'tests' / 'test_review_workflow.py').write_text('# tests\n', encoding='utf-8')

    def validate_main(self, document, *extra, config=True, repo=True):
        path = self.root / 'payload.json'
        path.write_text(json.dumps(document), encoding='utf-8')

        def endpoint(action, args):
            text = Path(args[2]).read_text(encoding='utf-8')
            result = cr.validate(['validate', '@attachment:0'], {'0': {'flag': '--file', 'text': text}},
                                 self.native, [OPERATOR])
            return {'returncode': 0, 'stdout': json.dumps(result), 'stderr': ''}

        repo_args = ['--repo', str(self.repo)] if repo else []
        return self.main('capability', 'validate', '--file', str(path), *repo_args, *extra,
                         config=config, request=endpoint)

    def test_client_combines_endpoint_preflight_with_local_pointer_checks(self):
        result = self.validate_main(without_operation(entry()))
        code, out, err, calls = result
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(calls, [('capability', ['validate', '--file', str(self.root / 'payload.json')])])
        payload = json.loads(out)
        self.assertTrue(payload['valid'])
        self.assertEqual(payload['summary'], {'items': 1, 'valid': 1, 'invalid': 0, 'errors': 0})
        self.assertTrue(all(pointer['resolved'] is True for pointer in payload['items'][0]['pointers']))
        self.assertEqual(self.native.writes(), [])

    def test_repo_defaults_to_the_current_checkout(self):
        previous = os.getcwd()
        os.chdir(self.repo)
        self.addCleanup(os.chdir, previous)
        code, out, _, _ = self.validate_main(without_operation(entry()), repo=False)
        self.assertEqual(code, 0)
        self.assertTrue(json.loads(out)['valid'])

    def test_missing_pointers_are_item_errors_and_later_items_still_run(self):
        first = without_operation(entry(code=['missing.py::nothing'], anchors=['docs/REVIEWS.md#missing']))
        second = without_operation(entry(operation_id='cap-2', key='review.second', name='Second'))
        code, out, _, _ = self.validate_main([first, second])
        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertFalse(payload['valid'])
        self.assertEqual(payload['summary']['invalid'], 1)
        self.assertEqual([item['key'] for item in payload['items']],
                         ['review.structured-contribution', 'review.second'])
        self.assertTrue(payload['items'][1]['valid'])
        self.assertEqual({error['field'] for error in payload['items'][0]['errors']}, {'code', 'anchors'})

    def test_validate_needs_config_but_help_does_not(self):
        code, _, err, calls = self.main('capability', 'validate', '--file', 'x.json', config=False)
        self.assertEqual((code, calls), (2, []))
        self.assertIn('--config', err)
        code, out, err, calls = self.main('capability', 'validate', '--help', config=False)
        self.assertEqual((code, err, calls), (0, '', []))
        self.assertIn('one propose payload or a list', out)


if __name__ == '__main__':
    unittest.main()
