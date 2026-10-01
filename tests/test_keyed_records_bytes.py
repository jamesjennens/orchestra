"""Byte compatibility of the keyed-record core extraction (kittrial-5bb.66).

The .41 design (section 12.1) makes this a hard requirement: extracting
`keyed_records.py` from `requirement_records.py` must leave every byte requirements
write unchanged - the canonical `requirement-revision-v1` record, the
`requirement-acceptance-v1` evidence and every `.requirement-requests/` and
`.requirement-backfills/` receipt - because an older kit must still read them and a
rollback must not invalidate them.

`tests/fixtures/requirement_records_ec5d659.py` is a frozen, unedited copy of
`requirement_records.py` at main ec5d659 (`git show ec5d659:requirement_records.py`),
the last revision before the extraction. Each scenario below runs twice, through that
copy and through the current module (now a spec on the shared core), against a fresh
fake bd with a fixed clock, and asserts identical results or refusal messages,
identical native argv sequences, identical record comment bytes on the final rows and
identical journal file bytes. Each side then reads the other's records and receipts.
"""
import copy
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

KIT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KIT))
sys.path.insert(0, str(KIT / 'tests'))
import requirement_records as current
from test_requirement_records import Native

_spec = importlib.util.spec_from_file_location(
    'requirement_records_ec5d659', KIT / 'tests' / 'fixtures' / 'requirement_records_ec5d659.py')
frozen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(frozen)

FIXED = time.struct_time((2026, 10, 1, 12, 0, 0, 3, 274, 0))


def acceptance(**extra):
    data = {'owners': ['owner-a', 'owner-b'], 'approvers': ['owner-a'], 'policy': 'any-owner',
            'decision_id': 'decision-1', 'evidence': 'review-1'}
    data.update(extra)
    return data


def draft(**extra):
    payload = dict(schema_version=1, operation_id='op-1', operation='draft', kind='requirement',
                   title='R01: Intent', key='R01', description='Statement of intent.',
                   acceptance_state='draft', parent='job-1')
    payload.update(extra)
    return {name: value for name, value in payload.items() if value is not None}


def revise(**extra):
    payload = dict(schema_version=1, operation_id='op-2', operation='revise', kind='requirement',
                   task='req-1', title='R01: Intent', key='R01', description='Revised statement.',
                   revision=2, acceptance_state='draft')
    payload.update(extra)
    return {name: value for name, value in payload.items() if value is not None}


# Each step: (callable name, args, kwargs, native tweaks applied before the step).
SCENARIOS = {
    'contributor draft creates a record': [
        ('apply', (draft(),), {}, {})],
    'draft then revise': [
        ('apply', (draft(),), {}, {}),
        ('apply', (revise(),), {}, {})],
    'operator accepts a revision with evidence': [
        ('apply', (draft(),), {}, {}),
        ('apply', (revise(operation_id='op-acc', acceptance_state='accepted',
                          acceptance=acceptance()),), {'operator': True}, {})],
    'operator demotes an accepted record': [
        ('apply', (draft(),), {}, {}),
        ('apply', (revise(operation_id='op-acc', acceptance_state='accepted',
                          acceptance=acceptance()),), {'operator': True}, {}),
        ('apply', (revise(operation_id='op-dem', revision=3, description='Demoted.',
                          acceptance=acceptance(decision_id='decision-2')),), {'operator': True}, {})],
    'direct accepted revision 1 through create': [
        ('apply', (draft(acceptance_state='accepted', acceptance=acceptance()),), {'operator': True}, {})],
    'operator accepts revision 1 on an untyped task': [
        ('seed', ('task-9',), {}, {}),
        ('apply', (draft(parent=None, task='task-9', acceptance_state='accepted',
                         acceptance=acceptance()),), {'operator': True}, {})],
    'brd-section draft without key': [
        ('apply', (draft(kind='brd-section', key=None, title='Overview'),), {}, {})],
    'identical retry is reconciled': [
        ('apply', (draft(),), {}, {}),
        ('apply', (draft(),), {}, {})],
    'lost create response then retry': [
        ('apply', (draft(),), {}, {'create_outcome': 'lost-response'}),
        ('apply', (draft(),), {}, {'create_outcome': 'ok'})],
    'create refused after preflight then reconcile released': [
        ('apply', (draft(),), {}, {'create_outcome': 'not-written'}),
        ('reconcile', ('op-1', 'ops', 'nothing was created', 'released'), {}, {'create_outcome': 'ok'}),
        ('apply', (draft(),), {}, {})],
    'revision comment not written then retry': [
        ('apply', (draft(),), {}, {}),
        ('apply', (revise(),), {}, {'comment_outcome': 'not-written'}),
        ('apply', (revise(),), {}, {'comment_outcome': 'ok'})],
    'evidence write fails then retry': [
        ('apply', (draft(),), {}, {}),
        ('apply', (revise(operation_id='op-acc', acceptance_state='accepted',
                          acceptance=acceptance()),), {'operator': True},
         {'fail_comment_prefix': 'Kind: requirement-acceptance-v1'}),
        ('apply', (revise(operation_id='op-acc', acceptance_state='accepted',
                          acceptance=acceptance()),), {'operator': True}, {'fail_comment_prefix': None})],
    'reconcile complete from native': [
        ('apply', (draft(),), {}, {'create_outcome': 'lost-response'}),
        ('reconcile', ('op-1', 'ops', 'confirmed native record', 'complete'), {'issue_id': 'req-1'},
         {'create_outcome': 'ok'}),
        ('reconcile', ('op-1', 'ops', 'confirmed native record', 'complete'), {'issue_id': 'req-1'}, {})],
    'reconcile refuses failed when a record exists': [
        ('apply', (draft(),), {}, {'create_outcome': 'lost-response'}),
        ('reconcile', ('op-1', 'ops', 'gone', 'failed'), {}, {'create_outcome': 'ok'})],
    'backfill draft and accepted with evidence': [
        ('apply', (draft(acceptance_state='accepted', acceptance=acceptance()),), {'operator': True}, {}),
        ('seed', ('legacy-1',), {}, {}),
        ('backfill', ({'schema_version': 1, 'operation_id': 'bf-1', 'records': [
            {'task': 'legacy-1', 'kind': 'requirement', 'acceptance_state': 'accepted',
             'evidence': 'decision-7'}]},), {}, {}),
        ('backfill', ({'schema_version': 1, 'operation_id': 'bf-1', 'records': [
            {'task': 'legacy-1', 'kind': 'requirement', 'acceptance_state': 'accepted',
             'evidence': 'decision-7'}]},), {}, {})],
    'refusals are identical': [
        ('apply', (draft(),), {}, {}),
        ('apply', (draft(operation_id='op-dup'),), {}, {}),
        ('apply', (revise(revision=5),), {}, {}),
        ('apply', (revise(acceptance=acceptance()),), {}, {}),
        ('apply', (revise(acceptance_state='accepted'),), {}, {}),
        ('apply', (revise(key='R02', operation_id='op-swap'),), {}, {}),
        ('apply', (dict(revise(), labels=['x']),), {}, {}),
        ('apply', (revise(operation_id='op-3', kind='brd-section', key=None),), {}, {}),
        ('apply', (revise(acceptance_state='accepted', acceptance=acceptance(),
                          operation_id='op-x'),), {'operator': True, 'operators': ['ops']}, {})],
}


def run_scenario(module, steps):
    with tempfile.TemporaryDirectory() as temp:
        project = Path(temp)
        native = Native()
        native.seed('job-1')
        outcomes = []
        with patch('time.gmtime', return_value=FIXED):
            for name, args, kwargs, tweaks in steps:
                for attribute, value in tweaks.items():
                    setattr(native, attribute, value)
                try:
                    if name == 'seed':
                        native.seed(*args)
                        outcome = ('seeded',)
                    elif name == 'apply':
                        outcome = ('ok', module.apply_native(copy.deepcopy(args[0]), 'alice', native,
                                                             project, **kwargs))
                    elif name == 'backfill':
                        outcome = ('ok', module.backfill(copy.deepcopy(args[0]), 'ops', native, project))
                    else:
                        outcome = ('ok', module.reconcile(project, *args, native, **kwargs))
                except (ValueError, RuntimeError) as error:
                    outcome = ('refused', type(error).__name__, str(error))
                outcomes.append(outcome)
        journals = {path.relative_to(project).as_posix(): path.read_bytes()
                    for path in sorted(project.rglob('*.json'))}
        comments = {row['id']: [comment['text'] for comment in row['comments']] for row in native.rows}
        labels = {row['id']: sorted(row['labels']) for row in native.rows}
        return {'outcomes': outcomes, 'calls': native.calls, 'comments': comments,
                'labels': labels, 'journals': journals, 'rows': copy.deepcopy(native.rows)}


class ByteCompatibilityTests(unittest.TestCase):
    def test_the_frozen_copy_is_the_pre_extraction_module(self):
        self.assertTrue(hasattr(frozen, '_prior_state'))
        self.assertFalse(hasattr(frozen, 'SPEC'))
        self.assertTrue(hasattr(current, 'SPEC'))

    def test_every_scenario_writes_identical_bytes(self):
        for title, steps in SCENARIOS.items():
            with self.subTest(scenario=title):
                old = run_scenario(frozen, steps)
                new = run_scenario(current, steps)
                self.assertEqual(new['outcomes'], old['outcomes'])
                self.assertEqual(new['calls'], old['calls'])
                self.assertEqual(new['comments'], old['comments'])
                self.assertEqual(new['labels'], old['labels'])
                self.assertEqual(sorted(new['journals']), sorted(old['journals']))
                for name in old['journals']:
                    self.assertEqual(new['journals'][name], old['journals'][name], name)

    def test_the_scenarios_cover_records_evidence_and_receipts(self):
        written = run_scenario(current, SCENARIOS['operator demotes an accepted record'])
        bodies = [text for texts in written['comments'].values() for text in texts]
        self.assertTrue(any(text.startswith('Kind: requirement-revision-v1\n') for text in bodies))
        self.assertTrue(any(text.startswith('Kind: requirement-acceptance-v1\n') for text in bodies))
        self.assertTrue(any(name.startswith('.requirement-requests/') for name in written['journals']))
        backfilled = run_scenario(current, SCENARIOS['backfill draft and accepted with evidence'])
        self.assertTrue(any(name.startswith('.requirement-backfills/') for name in backfilled['journals']))

    def test_each_side_reads_the_others_records_and_receipts(self):
        steps = SCENARIOS['operator demotes an accepted record']
        sides = {'frozen': run_scenario(frozen, steps), 'current': run_scenario(current, steps)}
        for writer, written in sides.items():
            for reader in (frozen, current):
                with self.subTest(writer=writer, reader=reader.__name__):
                    for row in written['rows']:
                        if row['id'] != 'req-1':
                            continue
                        revisions = reader.existing_revisions(row)
                        self.assertEqual(sorted(revisions), [1, 2, 3])
                        for record in revisions.values():
                            self.assertEqual(record['sha256'], current.content_hash(record))
                        self.assertEqual(sorted(reader.existing_acceptances(row)), [2, 3])
                    for name, raw in written['journals'].items():
                        reader.validate_receipt(json.loads(raw.decode('utf-8')),
                                                backfill=name.startswith('.requirement-backfills/'))
        self.assertEqual(sides['frozen']['comments'], sides['current']['comments'])

    def test_the_shared_receipt_schema_is_the_requirement_one(self):
        good = {'sha256': 'a' * 64, 'status': 'pending', 'actor': 'alice', 'id': 'req-1', 'revision': 1,
                'operation': 'propose', 'key': 'calendar.trading'}
        for module in (frozen, current):
            module.validate_receipt(dict(good))
            for bad in (dict(good, status='done'), dict(good, sha256='x'), dict(good, revision=0),
                        dict(good, actor=' '), dict(good, id=7)):
                with self.assertRaises(ValueError):
                    module.validate_receipt(bad)


if __name__ == '__main__':
    unittest.main()
