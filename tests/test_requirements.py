import copy
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import requirements
from requirements import ValidationError, canonical_bytes, content_hash, validate_manifest

BASELINE = ROOT / 'docs' / 'requirements-baseline.json'


def real():
    return json.loads(BASELINE.read_text(encoding='utf-8'))


def stamped(record):
    record = copy.deepcopy(record)
    record.pop('sha256', None)
    record['sha256'] = content_hash(record)
    return record


def stamped_manifest(manifest):
    manifest = copy.deepcopy(manifest)
    manifest.pop('sha256', None)
    manifest['narrative'] = [stamped(r) for r in manifest['narrative']]
    manifest['requirements'] = [stamped(r) for r in manifest['requirements']]
    manifest['sha256'] = content_hash(manifest)
    return manifest


def draft():
    return stamped_manifest(real())


def accepted():
    manifest = stamped_manifest(real())
    manifest['state'] = 'accepted'
    for record in manifest['narrative'] + manifest['requirements']:
        record['acceptance_state'] = 'accepted'
    manifest = stamped_manifest(manifest)
    acceptance = {
        'manifest_sha256': manifest['sha256'],
        'decision_id': 'kittrial-pth.17',
        'owners': ['james'],
        'approvers': ['james'],
        'policy': 'any-owner',
        'evidence': 'Owner accepted the requirements-driven direction on 2026-09-13.',
    }
    return manifest, acceptance


class HashTests(unittest.TestCase):
    def test_real_baseline_validates_and_hashes_reproduce(self):
        manifest = real()
        self.assertIsNone(validate_manifest(manifest))
        self.assertEqual(content_hash(manifest), manifest['sha256'])
        for record in manifest['narrative'] + manifest['requirements']:
            self.assertEqual(content_hash(record), record['sha256'])

    def test_canonical_bytes_are_exact(self):
        self.assertEqual(canonical_bytes({'b': 1, 'a': ['café']}), '{"a":["café"],"b":1}'.encode('utf-8'))
        self.assertEqual(canonical_bytes({'b': 0, 'a': 0}), b'{"a":0,"b":0}')

    def test_canonical_bytes_reject_nan_and_non_json_values(self):
        with self.assertRaises(ValidationError):
            canonical_bytes({'a': float('nan')})
        with self.assertRaises(ValidationError):
            canonical_bytes({'a': object()})
        with self.assertRaises(ValidationError):
            canonical_bytes({'a': float('inf')})

    def test_content_hash_excludes_only_top_level_sha256(self):
        manifest = draft()
        self.assertEqual(content_hash(manifest), manifest['sha256'])
        self.assertEqual(content_hash(dict(manifest, sha256='f' * 64)), manifest['sha256'])
        nested = copy.deepcopy(manifest)
        nested['requirements'][0]['sha256'] = 'a' * 64
        self.assertNotEqual(content_hash(nested), manifest['sha256'])

    def test_content_hash_requires_mapping(self):
        with self.assertRaises(ValidationError):
            content_hash(['not', 'a', 'mapping'])


class SchemaTests(unittest.TestCase):
    def assertRejects(self, manifest, acceptance=None, fragment=None):
        with self.assertRaises(ValidationError) as caught:
            validate_manifest(manifest, acceptance)
        if fragment:
            self.assertIn(fragment, str(caught.exception))

    def test_unknown_top_level_field_rejected(self):
        manifest = draft()
        manifest['note'] = 'extra'
        manifest = stamped_manifest(manifest)
        self.assertRejects(manifest, fragment='unknown field')

    def test_missing_top_level_field_rejected(self):
        manifest = draft()
        del manifest['authority']
        self.assertRejects(manifest, fragment='missing field')

    def test_unknown_record_field_rejected(self):
        manifest = draft()
        manifest['requirements'][0]['owner'] = 'james'
        manifest = stamped_manifest(manifest)
        self.assertRejects(manifest, fragment='unknown field')

    def test_schema_version_must_be_integer_one(self):
        for value in (True, 2, '1', 1.0):
            manifest = draft()
            manifest['schema_version'] = value
            manifest = stamped_manifest(manifest)
            self.assertRejects(manifest, fragment='schema_version')

    def test_baseline_name_must_be_safe_directory_component(self):
        for value in ('..', '.', '', 'a/b', 'a\\b', '-leading', 'x' * 81):
            manifest = draft()
            manifest['baseline'] = value
            manifest = stamped_manifest(manifest)
            self.assertRejects(manifest, fragment='baseline')

    def test_nonempty_string_fields_rejected(self):
        manifest = draft()
        manifest['canonical_project'] = '   '
        manifest = stamped_manifest(manifest)
        self.assertRejects(manifest, fragment='canonical_project')

    def test_state_must_be_draft_or_accepted(self):
        manifest = draft()
        manifest['state'] = 'published'
        manifest = stamped_manifest(manifest)
        self.assertRejects(manifest, fragment='state')

    def test_requirements_must_be_nonempty_list(self):
        manifest = draft()
        manifest['requirements'] = []
        self.assertRejects(manifest, fragment='requirements')
        manifest = draft()
        manifest['requirements'] = {}
        self.assertRejects(manifest, fragment='requirements')

    def test_narrative_must_be_list(self):
        manifest = draft()
        manifest['narrative'] = 'narrative'
        self.assertRejects(manifest, fragment='narrative')


class RecordTests(unittest.TestCase):
    def assertRejects(self, manifest, acceptance=None, fragment=None):
        with self.assertRaises(ValidationError) as caught:
            validate_manifest(manifest, acceptance)
        if fragment:
            self.assertIn(fragment, str(caught.exception))

    def test_revision_must_be_positive_integer_not_boolean(self):
        for value in (True, 0, -1, '1', 1.5):
            manifest = draft()
            manifest['requirements'][0]['revision'] = value
            manifest = stamped_manifest(manifest)
            self.assertRejects(manifest, fragment='revision')

    def test_rejection_like_acceptance_state_is_unsupported(self):
        for value in ('rejected', 'blocked', 'Accepted'):
            manifest = draft()
            manifest['requirements'][0]['acceptance_state'] = value
            manifest = stamped_manifest(manifest)
            self.assertRejects(manifest, fragment='acceptance_state')

    def test_record_hash_mismatch_detected(self):
        manifest = real()
        manifest['requirements'][0]['title'] += ' (tampered)'
        self.assertRejects(manifest, fragment='does not match its content')

    def test_record_hash_format_enforced(self):
        manifest = real()
        manifest['requirements'][0]['sha256'] = manifest['requirements'][0]['sha256'].upper()
        self.assertRejects(manifest, fragment='lowercase 64-character')
        manifest = real()
        manifest['requirements'][0]['sha256'] = 'abc'
        self.assertRejects(manifest, fragment='lowercase 64-character')

    def test_manifest_hash_mismatch_detected(self):
        manifest = draft()
        manifest['authority'] = 'Different authority text.'
        self.assertRejects(manifest, fragment='does not match its content')

    def test_duplicate_ids_and_keys_rejected(self):
        manifest = draft()
        twin = copy.deepcopy(manifest['requirements'][0])
        twin['id'] = 'kittrial-pth.99'
        manifest['requirements'].append(twin)
        manifest = stamped_manifest(manifest)
        self.assertRejects(manifest, fragment='duplicate requirement key')

        manifest = draft()
        twin = copy.deepcopy(manifest['requirements'][0])
        twin['key'] = 'R99'
        manifest['requirements'].append(twin)
        manifest = stamped_manifest(manifest)
        self.assertRejects(manifest, fragment='duplicate record id')

        manifest = draft()
        manifest['narrative'][0]['id'] = manifest['requirements'][0]['id']
        manifest = stamped_manifest(manifest)
        self.assertRejects(manifest, fragment='duplicate record id')


class ReferenceTests(unittest.TestCase):
    def assertRejects(self, manifest, acceptance=None, fragment=None):
        with self.assertRaises(ValidationError) as caught:
            validate_manifest(manifest, acceptance)
        if fragment:
            self.assertIn(fragment, str(caught.exception))

    def with_reference(self, reference):
        manifest = draft()
        target = manifest['requirements'][1]
        record = manifest['requirements'][0]
        record['references'] = [reference(target) if callable(reference) else reference]
        return stamped_manifest(manifest)

    @staticmethod
    def exact(target):
        return {'id': target['id'], 'revision': target['revision'], 'sha256': target['sha256']}

    def test_valid_reference_accepted(self):
        manifest = self.with_reference(self.exact)
        self.assertIsNone(validate_manifest(manifest))

    def test_no_cycle_rule_just_exact_reference_matching(self):
        # Exact-hash references make a fully consistent cycle unsatisfiable
        # inside one snapshot, so the validator applies no cycle rule of its
        # own: a mutual pair is reported only as a hash mismatch.
        manifest = draft()
        first, second = manifest['requirements'][0], manifest['requirements'][1]
        first['references'] = [{'id': second['id'], 'revision': second['revision'], 'sha256': second['sha256']}]
        second['references'] = [{'id': first['id'], 'revision': first['revision'], 'sha256': first['sha256']}]
        manifest = stamped_manifest(manifest)
        with self.assertRaises(ValidationError) as caught:
            validate_manifest(manifest)
        message = str(caught.exception)
        self.assertIn('sha256 does not match', message)
        self.assertNotIn('cycle', message.lower())

    def test_unknown_reference_target_rejected(self):
        manifest = self.with_reference(lambda target: {'id': 'kittrial-pth.404', 'revision': 1, 'sha256': target['sha256']})
        self.assertRejects(manifest, fragment='unknown record')

    def test_reference_revision_mismatch_rejected(self):
        manifest = self.with_reference(lambda target: {'id': target['id'], 'revision': target['revision'] + 1, 'sha256': target['sha256']})
        self.assertRejects(manifest, fragment='revision does not match')

    def test_reference_hash_mismatch_rejected(self):
        manifest = self.with_reference(lambda target: {'id': target['id'], 'revision': target['revision'], 'sha256': 'b' * 64})
        self.assertRejects(manifest, fragment='sha256 does not match')

    def test_duplicate_reference_rejected(self):
        manifest = draft()
        target = manifest['requirements'][1]
        manifest['requirements'][0]['references'] = [self.exact(target), self.exact(target)]
        manifest = stamped_manifest(manifest)
        self.assertRejects(manifest, fragment='repeats a reference')

    def test_implicit_latest_reference_rejected(self):
        manifest = self.with_reference(lambda target: {'id': target['id'], 'revision': target['revision']})
        self.assertRejects(manifest, fragment='missing field')

    def test_references_must_be_a_list_of_objects(self):
        manifest = draft()
        manifest['requirements'][0]['references'] = 'kittrial-pth.3'
        manifest = stamped_manifest(manifest)
        self.assertRejects(manifest, fragment='references must be a list')
        manifest = draft()
        manifest['requirements'][0]['references'] = ['kittrial-pth.3@1']
        manifest = stamped_manifest(manifest)
        self.assertRejects(manifest, fragment='must be an object')


class AcceptanceTests(unittest.TestCase):
    def assertRejects(self, manifest, acceptance=None, fragment=None):
        with self.assertRaises(ValidationError) as caught:
            validate_manifest(manifest, acceptance)
        if fragment:
            self.assertIn(fragment, str(caught.exception))

    def test_accepted_baseline_with_acceptance_is_valid(self):
        manifest, acceptance = accepted()
        self.assertIsNone(validate_manifest(manifest, acceptance))

    def test_single_owner_may_contribute_and_approve(self):
        manifest, acceptance = accepted()
        acceptance['owners'] = ['james', 'other']
        acceptance['approvers'] = ['james']
        acceptance['policy'] = 'any-owner'
        self.assertIsNone(validate_manifest(manifest, acceptance))

    def test_draft_must_not_carry_acceptance(self):
        manifest = real()
        _, acceptance = accepted()
        acceptance['manifest_sha256'] = manifest['sha256']
        self.assertRejects(manifest, acceptance, fragment='draft manifest must not carry')

    def test_accepted_requires_all_records_accepted(self):
        manifest, acceptance = accepted()
        manifest['narrative'][0]['acceptance_state'] = 'draft'
        manifest = stamped_manifest(manifest)
        self.assertRejects(manifest, acceptance, fragment='must be accepted')

    def test_accepted_requires_acceptance_object(self):
        manifest, _ = accepted()
        self.assertRejects(manifest, None, fragment='requires an acceptance object')

    def test_prose_does_not_imply_acceptance(self):
        manifest = real()
        self.assertIn('Accepted workflow direction', manifest['requirements'][2]['description'])
        manifest['state'] = 'accepted'
        manifest = stamped_manifest(manifest)
        self.assertRejects(manifest, None, fragment='must be accepted')

    def test_acceptance_manifest_sha256_must_match(self):
        manifest, acceptance = accepted()
        acceptance['manifest_sha256'] = '0' * 64
        self.assertRejects(manifest, acceptance, fragment='manifest_sha256')

    def test_acceptance_fields_are_exact(self):
        manifest, acceptance = accepted()
        acceptance['approval_date'] = '2026-09-13'
        self.assertRejects(manifest, acceptance, fragment='unknown field')
        manifest, acceptance = accepted()
        del acceptance['evidence']
        self.assertRejects(manifest, acceptance, fragment='missing field')

    def test_acceptance_owner_and_approver_lists(self):
        manifest, acceptance = accepted()
        acceptance['owners'] = []
        self.assertRejects(manifest, acceptance, fragment='owners')
        manifest, acceptance = accepted()
        acceptance['owners'] = ['james', 'james']
        self.assertRejects(manifest, acceptance, fragment='duplicate')
        manifest, acceptance = accepted()
        acceptance['owners'] = ['james', 'other']
        acceptance['approvers'] = ['someone-else']
        self.assertRejects(manifest, acceptance, fragment='subset')
        manifest, acceptance = accepted()
        acceptance['owners'] = ['james', 'other']
        acceptance['approvers'] = ['james']
        acceptance['policy'] = 'all-owners'
        self.assertRejects(manifest, acceptance, fragment='all-owners')
        manifest, acceptance = accepted()
        acceptance['policy'] = 'some-owners'
        self.assertRejects(manifest, acceptance, fragment='policy')

    def test_validation_does_not_mutate_or_accept_draft(self):
        manifest = real()
        before = json.dumps(manifest, sort_keys=True)
        self.assertIsNone(validate_manifest(manifest))
        self.assertEqual(json.dumps(manifest, sort_keys=True), before)
        self.assertEqual(manifest['state'], 'draft')


class CliTests(unittest.TestCase):
    def run_cli(self, *extra):
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        return subprocess.run(
            [sys.executable, '-X', 'utf8', str(ROOT / 'requirements.py'), *extra],
            capture_output=True,
            text=True,
            env=env,
        )

    def test_valid_manifest_exits_zero_quietly(self):
        result = self.run_cli(str(BASELINE))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, '')

    def test_malformed_manifest_exits_nonzero_without_traceback(self):
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / 'bad.json'
            manifest = draft()
            manifest['state'] = 'published'
            manifest = stamped_manifest(manifest)
            bad.write_text(json.dumps(manifest), encoding='utf-8')
            result = self.run_cli(str(bad))
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Traceback', result.stderr)
            self.assertIn('invalid manifest', result.stderr)

    def test_missing_file_and_bad_json_exit_nonzero(self):
        result = self.run_cli(str(ROOT / 'docs' / 'does-not-exist.json'))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)
        with tempfile.TemporaryDirectory() as folder:
            bad = Path(folder) / 'broken.json'
            bad.write_text('{"schema_version": ', encoding='utf-8')
            result = self.run_cli(str(bad))
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Traceback', result.stderr)

    def test_acceptance_flag_is_used(self):
        manifest, acceptance = accepted()
        with tempfile.TemporaryDirectory() as folder:
            manifest_path = Path(folder) / 'manifest.json'
            acceptance_path = Path(folder) / 'acceptance.json'
            manifest_path.write_text(json.dumps(manifest), encoding='utf-8')
            acceptance_path.write_text(json.dumps(acceptance), encoding='utf-8')
            result = self.run_cli(str(manifest_path), '--acceptance', str(acceptance_path))
            self.assertEqual(result.returncode, 0, result.stderr)
            acceptance['manifest_sha256'] = '0' * 64
            acceptance_path.write_text(json.dumps(acceptance), encoding='utf-8')
            result = self.run_cli(str(manifest_path), '--acceptance', str(acceptance_path))
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('Traceback', result.stderr)
        result = self.run_cli(str(BASELINE), '--acceptance', str(BASELINE))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Traceback', result.stderr)


if __name__ == '__main__':
    unittest.main()
